# -*- coding: utf-8 -*-
"""
/***************************************************************************
 Flo2D
                                 A QGIS plugin
 FLO-2D tools for QGIS
                             -------------------
        begin                : 2016-08-28
        copyright            : (C) 2016 by Lutra Consulting for FLO-2D
        email                : info@lutraconsulting.co.uk
        git sha              : $Format:%H$
 ***************************************************************************/

/***************************************************************************
 *                                                                         *
 *   This program is free software; you can redistribute it and/or modify  *
 *   it under the terms of the GNU General Public License as published by  *
 *   the Free Software Foundation; either version 2 of the License, or     *
 *   (at your option) any later version.                                   *
 *                                                                         *
 ***************************************************************************/
 This script initializes the plugin, making it known to QGIS.
"""
import os
import traceback
from operator import itemgetter
from itertools import chain, groupby, izip
from flo2d_parser import ParseDAT
from user_communication import UserCommunication

def spatialite_connect(*args, **kwargs):
    # copied from https://github.com/qgis/QGIS/blob/master/python/utils.py#L587
    try:
        from pyspatialite import dbapi2
    except ImportError:
        import sqlite3
        con = sqlite3.dbapi2.connect(*args, **kwargs)
        con.enable_load_extension(True)
        cur = con.cursor()
        libs = [
            # Spatialite >= 4.2 and Sqlite >= 3.7.17, should work on all platforms
            ("mod_spatialite", "sqlite3_modspatialite_init"),
            # Spatialite >= 4.2 and Sqlite < 3.7.17 (Travis)
            ("mod_spatialite.so", "sqlite3_modspatialite_init"),
            # Spatialite < 4.2 (linux)
            ("libspatialite.so", "sqlite3_extension_init")
        ]
        found = False
        for lib, entry_point in libs:
            try:
                cur.execute("select load_extension('{}', '{}')".format(lib, entry_point))
            except sqlite3.OperationalError:
                continue
            else:
                found = True
                break
        if not found:
            raise RuntimeError("Cannot find any suitable spatialite module")
        cur.close()
        con.enable_load_extension(False)
        return con
    return dbapi2.connect(*args, **kwargs)

class GeoPackageUtils(object):
    """GeoPackage utils for handling GPKG files"""
    def __init__(self, path, iface):
        self.path = path
        self.iface = iface
        self.uc = UserCommunication(iface, 'FLO-2D')
        self.conn = None
        self.msg = None

    def database_create(self):
        """Create geopackage with SpatiaLite functions"""
        # delete db file if exists
        if os.path.exists(self.path):
            try:
                os.remove(self.path)
            except:
                self.msg = "Couldn't write on the existing GeoPackage file. Check if it is not opened by another process."
                return False
        self.conn = self.database_connect()
        plugin_dir = os.path.dirname(__file__)
        script = os.path.join(plugin_dir, 'db_structure.sql')
        qry = open(script, 'r').read()
        c = self.conn.cursor()
        c.executescript(qry)
        self.conn.commit()
        c.close()
        return True

    def database_connect(self):
        """Connect database with sqlite3"""
        try:
            self.conn = spatialite_connect(self.path)
            return True
        except Exception as e:
            self.msg = 'Couldn\'t connect to GeoPackage'
            return False

    def database_disconnect(self):
        """Disconnect from database"""
        try:
            if self.conn is not None:
                self.conn.close()
            else:
                self.msg = 'There is no opened connection!'
        except Exception as e:
            raise

    def check_gpkg(self):
        """Check if file is GeoPackage """
        try:
            c = self.conn.cursor()
            c.execute('SELECT * FROM gpkg_contents;')
            c.fetchone()
            return True
        except:
            return False

    def execute(self, statement, inputs=None):
        """Execute a prepared SQL statement on this geopackage database."""
        with self.conn as db_con:
            cursor = db_con.cursor()
            if inputs is not None:
                result_cursor = cursor.execute(statement, inputs)
            else:
                result_cursor = cursor.execute(statement)
            return result_cursor

    def batch_execute(self, *sqls):
        for sql in sqls:
            qry = None
            if len(sql) == 1:
                continue
            else:
                pass
            try:
                qry = sql.pop(0)
                qry += ','.join(sql)
                del sql[:]
                self.execute(qry)
            except Exception as e:
                self.uc.log_info(qry)
                self.uc.log_info(traceback.format_exc())

    def is_table_empty(self, table):
        r = self.execute('''SELECT rowid FROM {0};'''.format(table))
        if r.fetchone():
            return False
        else:
            return True

    def clear_tables(self, *tables):
        for tab in tables:
            if not self.is_table_empty(tab):
                sql = '''DELETE FROM "{0}";'''.format(tab)
                self.execute(sql)
            else:
                pass

    def get_centroids(self, gids, table='grid', field='fid'):
        cells = {}
        for g in set(gids):
            sql = '''SELECT ST_AsText(ST_Centroid(GeomFromGPB(geom))) FROM "{0}" WHERE "{1}" = {2};'''.format(table, field, g)
            wkt_geom = self.execute(sql).fetchone()[0]
            cells[g] = wkt_geom
        return cells

    def build_linestring(self, gids, table='grid', field='fid'):
        gpb = '''AsGPB(ST_GeomFromText('LINESTRING('''
        points = []
        for g in gids:
            qry = '''SELECT ST_AsText(ST_Centroid(GeomFromGPB(geom))) FROM "{0}" WHERE "{1}" = {2};'''.format(table, field, g)
            wkt_geom = self.execute(qry).fetchone()[0]
            points.append(wkt_geom.strip('POINT()'))
        gpb = gpb + ','.join(points) + ')\'))'
        return gpb

    def build_multilinestring(self, gid, directions, cellsize, table='grid', field='fid'):
        functions = {
            '1': (lambda x, y, shift: (x, y + shift)),
            '2': (lambda x, y, shift: (x + shift, y)),
            '3': (lambda x, y, shift: (x, y - shift)),
            '4': (lambda x, y, shift: (x - shift, y)),
            '5': (lambda x, y, shift: (x + shift, y + shift)),
            '6': (lambda x, y, shift: (x + shift, y - shift)),
            '7': (lambda x, y, shift: (x - shift, y - shift)),
            '8': (lambda x, y, shift: (x - shift, y + shift))
        }
        gpb = '''AsGPB(ST_GeomFromText('MULTILINESTRING('''
        gpb_part = '''({0} {1}, {2} {3})'''
        qry = '''SELECT ST_AsText(ST_Centroid(GeomFromGPB(geom))) FROM "{0}" WHERE "{1}" = {2};'''.format(table, field, gid)
        wkt_geom = self.execute(qry).fetchone()[0]
        x1, y1 = [float(i) for i in wkt_geom.strip('POINT()').split()]
        half_cell = cellsize * 0.5
        parts = []
        for d in directions:
            x2, y2 = functions[d](x1, y1, half_cell)
            parts.append(gpb_part.format(x1, y1, x2, y2))
        gpb = gpb + ','.join(parts) + ')\'))'
        return gpb

    def build_levee(self, gid, direction, cellsize, table='grid', field='fid'):
        functions = {
            '1': (lambda x, y, s: (x - s/2.414, y + s, x + s/2.414, y + s)),
            '2': (lambda x, y, s: (x + s, y + s/2.414, x + s, y - s/2.414)),
            '3': (lambda x, y, s: (x + s/2.414, y - s, x - s/2.414, y - s)),
            '4': (lambda x, y, s: (x - s, y - s/2.414, x - s, y + s/2.414)),
            '5': (lambda x, y, s: (x + s/2.414, y + s, x + s, y + s/2.414)),
            '6': (lambda x, y, s: (x + s, y - s/2.414, x + s/2.414, y - s)),
            '7': (lambda x, y, s: (x - s/2.414, y - s, x - s, y - s/2.414)),
            '8': (lambda x, y, s: (x - s, y + s/2.414, x - s/2.414, y + s))
        }
        qry = '''SELECT ST_AsText(ST_Centroid(GeomFromGPB(geom))) FROM "{0}" WHERE "{1}" = {2};'''.format(table, field, gid)
        wkt_geom = self.execute(qry).fetchone()[0]
        xc, yc = [float(i) for i in wkt_geom.strip('POINT()').split()]
        x1, y1, x2, y2 = functions[direction](xc, yc, cellsize*0.48)
        gpb = '''AsGPB(ST_GeomFromText('LINESTRING({0} {1}, {2} {3})'))'''.format(x1, y1, x2, y2)
        return gpb

    @staticmethod
    def build_buffer(wkt_geom, distance, quadrantsegments=3):
        gpb = '''AsGPB(ST_Buffer(ST_GeomFromText('{0}'), {1}, {2}))'''
        gpb = gpb.format(wkt_geom, distance, quadrantsegments)
        return gpb

    @staticmethod
    def build_square(wkt_geom, size):
        x, y = [float(x) for x in wkt_geom.strip('POINT()').split()]
        half_size = float(size) * 0.5
        gpb = '''AsGPB(ST_GeomFromText('POLYGON(({} {}, {} {}, {} {}, {} {}, {} {}))'))'''.format(
            x - half_size, y - half_size,
            x + half_size, y - half_size,
            x + half_size, y + half_size,
            x - half_size, y + half_size,
            x - half_size, y - half_size
        )
        return gpb

    def get_max(self, table, field='fid'):
        sql = '''SELECT MAX("{0}") FROM "{1}";'''.format(field, table)
        max_val = self.execute(sql).fetchone()[0]
        max_val = 0 if max_val is None else max_val
        return max_val


class Flo2dGeoPackage(GeoPackageUtils):
    """GeoPackage object class for storing FLO-2D model data"""
    def __init__(self, path, iface):
        super(Flo2dGeoPackage, self).__init__(path, iface)
        self.group = 'FLO-2D_{}'.format(os.path.basename(path).replace('.gpkg', ''))
        self.parser = None
        self.cell_size = None
        self.buffer = None
        self.shrink = None
        self.chunksize = float('inf')

    def set_parser(self, fpath):
        self.parser = ParseDAT()
        self.parser.scan_project_dir(fpath)
        self.cell_size = self.parser.calculate_cellsize()
        if self.cell_size == 0:
            self.uc.bar_error('Cell size is 0 - something went wrong!')
        else:
            pass
        self.buffer = self.cell_size * 0.4
        self.shrink = self.cell_size * 0.95

    def import_cont_toler(self):
        sql = ['''INSERT INTO cont (name, value) VALUES''']
        sql_part = '''\n('{0}', '{1}')'''
        self.clear_tables('cont', 'time_series', 'time_series_data')
        cont = self.parser.parse_cont()
        toler = self.parser.parse_toler()
        cont.update(toler)
        for option in cont:
            sql += [sql_part.format(option, cont[option]).replace("'None'", "NULL")]
        sql += [sql_part.format('CELLSIZE', self.cell_size)]

        self.batch_execute(sql)

    def import_mannings_n_topo(self):
        # insert grid data into gpkg
        sql = ['''INSERT INTO grid (fid, n_value, elevation, geom) VALUES''']

        self.clear_tables('grid')
        data = self.parser.parse_mannings_n_topo()

        c = 0
        for d in data:
            man = slice(0, 2)
            coords = slice(2, 4)
            elev = slice(4, None)
            if c < self.chunksize:
                geom = ' '.join(d[coords])
                g = self.build_square(geom, self.cell_size)
                sql += ['\n({0}, {1})'.format(','.join(d[man] + d[elev]), g)]
                c += 1
            else:
                qry = sql[0] + ','.join(sql[1:])
                del sql[1:]
                self.execute(qry)
                del qry
                c = 0
        if len(sql) > 1:
            qry = sql[0] + ','.join(sql[1:])
            del sql[1:]
            self.execute(qry)
        else:
            pass

    def import_inflow(self):
        cont_sql = ['''INSERT INTO cont (name, value) VALUES''']
        inflow_sql = ['''INSERT INTO inflow (time_series_fid, ident, inoutfc, geom) VALUES''']
        cells_sql = ['''INSERT INTO inflow_cells (inflow_fid, grid_fid) VALUES''']
        ts_sql = ['''INSERT INTO time_series (hourdaily) VALUES''']
        tsd_sql = ['''INSERT INTO time_series_data (series_fid, time, value, value2) VALUES''']
        reservoirs_sql = ['''INSERT INTO reservoirs (grid_fid, wsel, geom) VALUES''']

        cont_part = '''\n('IDEPLT', '{0}')'''
        inflow_part = '''\n({0}, '{1}', {2}, {3})'''
        cells_part = '''\n({0}, {1})'''
        ts_part = '''\n({0})'''
        tsd_part = '''\n({0}, {1}, {2}, {3})'''
        reservoirs_part = '''\n({0}, {1}, {2})'''

        self.clear_tables('inflow', 'inflow_cells', 'reservoirs')
        head, inf, res = self.parser.parse_inflow()
        cont_sql += [cont_part.format(head['IHOURDAILY'])]
        gids = inf.keys() + res.keys()
        cells = self.get_centroids(gids)
        ts_max = self.get_max('time_series')
        values = slice(1, None)
        for i, gid in enumerate(inf, 1):
            row = inf[gid]['row']
            inflow_sql += [inflow_part.format(i + ts_max, row[0], row[1], self.build_buffer(cells[gid], self.buffer))]
            cells_sql += [cells_part.format(i, gid)]
            ts_sql += [ts_part.format(head['IHOURDAILY'])]
            for n in inf[gid]['time_series']:
                tsd_sql += [tsd_part.format(i + ts_max, *n[values])]

        for gid in res:
            row = res[gid]['row']
            wsel = row[-1] if len(row) == 3 else 'NULL'
            reservoirs_sql += [reservoirs_part.format(row[1], wsel, self.build_buffer(cells[gid], self.buffer))]

        self.batch_execute(cont_sql, ts_sql, inflow_sql, cells_sql, tsd_sql, reservoirs_sql)

    def import_outflow(self):
        outflow_sql = ['''INSERT INTO outflow (geom, time_series_fid, qh_params_fid, qh_table_fid, ident, nostacfp) VALUES''']
        cells_sql = ['''INSERT INTO outflow_cells (outflow_fid, grid_fid) VALUES''']
        chan_sql = ['''INSERT INTO outflow_chan_elems (outflow_fid, elem_fid) VALUES''']
        qh_sql = ['''INSERT INTO qh_params (hmax, coef, exponent) VALUES''']
        qh_tab_sql = ['''INSERT INTO qh_table (fid) VALUES''']
        qh_data_sql = ['''INSERT INTO qh_table_data (table_fid, depth, q) VALUES''']
        ts_sql = ['''INSERT INTO time_series (fid) VALUES''']
        ts_data_sql = ['''INSERT INTO time_series_data (series_fid, time, value) VALUES''']
        hydchar_sql = ['''INSERT INTO outflow_hydrographs (hydro_fid, grid_fid) VALUES''']

        outflow_part = '''\n({0}, {1}, {2}, {3}, '{4}', {5})'''
        cells_chan_part = '''\n({0}, {1})'''
        qh_part = '''\n({0}, {1}, {2})'''
        qh_tab_part = '''\n({0})'''
        qh_data_part = '''\n({0}, {1}, {2})'''
        ts_part = '''\n({0})'''
        ts_data_part = '''\n({0}, {1}, {2})'''
        hydchar_part_sql = '''\n('{0}', {1})'''

        self.clear_tables('outflow', 'outflow_hydrographs', 'qh_params', 'qh_table', 'qh_table_data')
        data = self.parser.parse_outflow()
        gids = (row[0] if row[0].isdigit() else row[1] for row in chain(data['K'], data['N'], data['O']))
        cells = self.get_centroids(gids)

        qhfid = 0
        qhtabfid = 0
        ts_max = self.get_max('time_series')

        for i, row in enumerate(data['K'], 1):
            gid = row[0]
            series = row[-1]
            geom = self.build_buffer(cells[gid], self.buffer)
            qh_params = series['qh_params']
            qh_data = series['qh_data']
            if qh_params:
                qhfid += 1
                qh_sql += [qh_part.format(*qh_params)]
            else:
                pass
            if qh_data:
                qhtabfid += 1
                qh_tab_sql += [qh_tab_part.format(qhtabfid)]
            else:
                pass
            outflow_sql += [outflow_part.format(geom, 'NULL', qhfid if qh_params else 'NULL', qhtabfid if qh_data else 'NULL', 'K', 'NULL')]
            chan_sql += [cells_chan_part.format(i, gid)]
            for trow in series['qh_data']:
                qh_data_sql += [qh_data_part.format(qhtabfid, *trow)]

        start = len(data['K']) + 1
        for i, row in enumerate(data['N'], start):
            gid, nostacfp, series = row
            geom = self.build_buffer(cells[gid], self.buffer)
            time_series = series['time_series']
            if time_series:
                ts_max += 1
                ts_sql += [ts_part.format(ts_max)]
            else:
                pass
            outflow_sql += [outflow_part.format(geom, ts_max if time_series else 'NULL', 'NULL', 'NULL', 'N', nostacfp)]
            cells_sql += [cells_chan_part.format(i, gid)]
            for srow in time_series:
                ts_data_sql += [ts_data_part.format(ts_max, *srow)]

        for row in data['O']:
            hydchar_sql += [hydchar_part_sql.format(*row)]

        self.batch_execute(ts_sql, ts_data_sql, qh_sql, qh_tab_sql, qh_data_sql, outflow_sql, chan_sql, cells_sql, hydchar_sql)

    def import_rain(self):
        rain_sql = ['''INSERT INTO rain (time_series_fid, irainreal, irainbuilding, tot_rainfall, rainabs, irainarf, movingstrom, rainspeed, iraindir) VALUES''']
        ts_sql = ['''INSERT INTO time_series (fid) VALUES''']
        tsd_sql = ['''INSERT INTO time_series_data (series_fid, time, value) VALUES''']
        rain_arf_sql = ['''INSERT INTO rain_arf_areas (rain_fid, arf, geom) VALUES''']
        cells_sql = ['''INSERT INTO rain_arf_cells (rain_arf_area_fid, grid_fid, arf) VALUES''']

        rain_part = '''\n({0}, {1}, {2}, {3}, {4}, {5}, {6}, {7}, {8})'''
        ts_part = '''\n({0})'''
        tsd_part = '''\n({0}, {1}, {2})'''
        rain_arf_part = '''\n({0}, {1}, {2})'''
        cells_part = '''\n({0}, {1}, {2})'''

        self.clear_tables('rain', 'rain_arf_areas', 'rain_arf_cells')
        options, time_series, rain_arf = self.parser.parse_rain()
        gids = (x[0] for x in rain_arf)
        cells = self.get_centroids(gids)

        fid = 1
        fid_ts = self.get_max('time_series') + 1

        rain_sql += [rain_part.format(fid_ts, *options.values())]
        ts_sql += [ts_part.format(fid_ts)]

        for row in time_series:
            char, time, value = row
            tsd_sql += [tsd_part.format(fid_ts, time, value)]

        for i, row in enumerate(rain_arf, 1):
            gid, val = row
            rain_arf_sql += [rain_arf_part.format(fid, val, self.build_buffer(cells[gid], self.buffer))]
            cells_sql += [cells_part.format(i, gid, val)]

        self.batch_execute(ts_sql, rain_sql, tsd_sql, rain_arf_sql, cells_sql)

    def import_infil(self):
        infil_params = ['infmethod', 'abstr', 'sati', 'satf', 'poros', 'soild', 'infchan', 'hydcall', 'soilall', 'hydcadj', 'hydcxx', 'scsnall', 'abstr1', 'fhortoni', 'fhortonf', 'decaya']
        infil_sql = ['INSERT INTO infil (' + ', '.join(infil_params) + ') VALUES']
        infil_seg_sql = ['''INSERT INTO infil_chan_seg (chan_seg_fid, hydcx, hydcxfinal, soildepthcx) VALUES''']
        infil_green_sql = ['''INSERT INTO infil_areas_green (geom, hydc, soils, dtheta, abstrinf, rtimpf, soil_depth) VALUES''']
        infil_scs_sql = ['''INSERT INTO infil_areas_scs (geom, scscn) VALUES''']
        infil_horton_sql = ['''INSERT INTO infil_areas_horton (geom, fhorti, fhortf, deca) VALUES''']
        infil_chan_sql = ['''INSERT INTO infil_areas_chan (geom, hydconch) VALUES''']

        cells_green_sql = ['''INSERT INTO infil_cells_green (infil_area_fid, grid_fid) VALUES''']
        cells_scs_sql = ['''INSERT INTO infil_cells_scs (infil_area_fid, grid_fid) VALUES''']
        cells_horton_sql = ['''INSERT INTO infil_cells_horton (infil_area_fid, grid_fid) VALUES''']
        chan_sql = ['''INSERT INTO infil_chan_elems (infil_area_fid, grid_fid) VALUES''']

        seg_part = '\n({0}, {1}, {2}, {3})'
        green_part = '''\n({0}, {1}, {2}, {3}, {4}, {5}, {6})'''
        scs_part = '''\n({0}, {1})'''
        chan_part = '''\n({0}, {1})'''
        horton_part = '''\n({0}, {1}, {2}, {3})'''
        cells_chan_part = '''\n({0}, {1})'''

        sqls = {
            'F': [infil_green_sql, green_part, cells_green_sql],
            'S': [infil_scs_sql, scs_part, cells_scs_sql],
            'H': [infil_horton_sql, horton_part, cells_horton_sql],
            'C': [infil_chan_sql, chan_part, chan_sql]
        }

        self.clear_tables('infil', 'infil_chan_seg',
                          'infil_areas_green', 'infil_areas_scs', 'infil_areas_horton ', 'infil_areas_chan',
                          'infil_cells_green', 'infil_cells_scs', 'infil_cells_horton', 'infil_chan_elems')
        data = self.parser.parse_infil()

        infil_sql += ['(' + ', '.join([data[k.upper()] if k.upper() in data else 'NULL' for k in infil_params]) + ')']
        gids = (x[0] for x in chain(data['F'], data['S'], data['C'], data['H']))
        cells = self.get_centroids(gids)

        for i, row in enumerate(data['R'], 1):
            infil_seg_sql += [seg_part.format(i, *row)]

        for k in sqls:
            if len(data[k]) > 0:
                for i, row in enumerate(data[k], 1):
                    gid = row[0]
                    geom = self.build_square(cells[gid], self.shrink)
                    sqls[k][0] += [sqls[k][1].format(geom, *row[1:])]
                    sqls[k][-1] += [cells_chan_part.format(i, gid)]
            else:
                pass

        self.batch_execute(infil_sql, infil_seg_sql, infil_green_sql, infil_scs_sql, infil_horton_sql, infil_chan_sql,
                           cells_green_sql, cells_scs_sql, cells_horton_sql, chan_sql)

    def import_evapor(self):
        evapor_sql = ['''INSERT INTO evapor (ievapmonth, iday, clocktime) VALUES''']
        evapor_month_sql = ['''INSERT INTO evapor_monthly (month, monthly_evap) VALUES''']
        evapor_hour_sql = ['''INSERT INTO evapor_hourly (month, hour, hourly_evap) VALUES''']

        evapor_part = '''\n({0}, {1}, {2})'''
        evapor_month_part = '''\n('{0}', {1})'''
        evapor_hour_part = '''\n('{0}', {1}, {2})'''

        self.clear_tables('evapor', 'evapor_monthly', 'evapor_hourly')
        head, data = self.parser.parse_evapor()
        evapor_sql += [evapor_part.format(*head)]
        for month in data:
            row = data[month]['row']
            time_series = data[month]['time_series']
            evapor_month_sql += [evapor_month_part.format(*row)]
            for i, ts in enumerate(time_series, 1):
                evapor_hour_sql += [evapor_hour_part.format(month, i, ts)]

        self.batch_execute(evapor_sql, evapor_month_sql, evapor_hour_sql)

    def import_chan(self):
        chan_sql = ['''INSERT INTO chan (geom, depinitial, froudc, roughadj, isedn) VALUES''']
        chan_elems_sql = ['''INSERT INTO chan_elems (geom, fid, seg_fid, nr_in_seg, rbankgrid, fcn, xlen, type) VALUES''']
        chan_r_sql = ['''INSERT INTO chan_r (elem_fid, bankell, bankelr, fcw, fcd) VALUES''']
        chan_v_sql = ['''INSERT INTO chan_v (elem_fid, bankell, bankelr, fcd, a1, a2, b1, b2, c1, c2, excdep, a11, a22, b11, b22, c11, c22) VALUES''']
        chan_t_sql = ['''INSERT INTO chan_t (elem_fid, bankell, bankelr, fcw, fcd, zl, zr) VALUES''']
        chan_n_sql = ['''INSERT INTO chan_n (elem_fid, nxsecnum, xsecname) VALUES''']
        chan_wsel_sql = ['''INSERT INTO chan_wsel (istart, wselstart, iend, wselend) VALUES''']
        chan_conf_sql = ['''INSERT INTO chan_confluences (geom, conf_fid, type, chan_elem_fid) VALUES''']
        chan_e_sql = ['''INSERT INTO noexchange_chan_areas (geom) VALUES''']
        elems_e_sql = ['''INSERT INTO noexchange_chan_elems (noex_area_fid, chan_elem_fid) VALUES''']

        chan_part = '''\n({0}, {1}, {2}, {3}, {4})'''
        chan_elems_part = '''\n({0}, {1}, {2}, {3}, {4}, {5}, {6}, '{7}')'''
        chan_r_part = '\n(' + ','.join(['{} '] * 10) + ')'
        chan_v_part = '\n(' + ','.join(['{} '] * 17) + ')'
        chan_t_part = '\n(' + ','.join(['{} '] * 7) + ')'
        chan_n_part = '\n(' + ','.join(['{} '] * 3) + ')'
        chan_wsel_part = '\n(' + ','.join(['{} '] * 4) + ')'
        chan_conf_part = '\n(' + ','.join(['{} '] * 4) + ')'
        chan_e_part = '''\n({0})'''
        elems_part = '''\n({0}, {1})'''

        sqls = {
            'R': [chan_r_sql, chan_r_part, 4, 7],
            'V': [chan_v_sql, chan_v_part, 4, 6],
            'T': [chan_t_sql, chan_t_part, 4, 7],
            'N': [chan_n_sql, chan_n_part, 2, 3]
        }

        self.clear_tables('chan', 'chan_elems', 'chan_r', 'chan_v', 'chan_t', 'chan_n',
                          'chan_confluences', 'noexchange_chan_areas', 'noexchange_chan_elems', 'chan_wsel')
        segments, wsel, confluence, noexchange = self.parser.parse_chan()
        for i, seg in enumerate(segments, 1):
            xs = seg[-1]
            gids = []
            for ii, row in enumerate(xs, 1):
                char = row[0]
                gid = row[1]
                rbank = row[-1]
                geom = self.build_linestring([gid, rbank]) if int(rbank) > 0 else 'NULL'
                sql, sql_part, fcn_idx, xlen_idx = sqls[char]
                xlen = row.pop(xlen_idx)
                fcn = row.pop(fcn_idx)
                params = row[1:-1]
                gids.append(gid)
                chan_elems_sql += [chan_elems_part.format(geom, gid, i, ii, rbank, fcn, xlen, char)]
                sql += [sql_part.format(*params)]
            options = seg[:-1]
            geom = self.build_linestring(gids)
            chan_sql += [chan_part.format(geom, *options)]

        for row in wsel:
            chan_wsel_sql += [chan_wsel_part.format(*row)]

        for i, row in enumerate(confluence, 1):
            gid1, gid2 = row[1], row[2]
            cells = self.get_centroids([gid1, gid2])
            geom1, geom2 = ["AsGPB(ST_GeomFromText('{}'))".format(g) for g in [cells[gid1], cells[gid2]]]
            chan_conf_sql += [chan_conf_part.format(geom1, i, 0, gid1)]
            chan_conf_sql += [chan_conf_part.format(geom2, i, 1, gid2)]

        for i, row in enumerate(noexchange, 1):
            gid = row[-1]
            geom = self.get_centroids([gid])[0]
            chan_e_sql += [chan_e_part.format(self.build_buffer(geom, self.buffer))]
            elems_e_sql += [elems_part.format(i, gid)]

        self.batch_execute(chan_sql, chan_elems_sql, chan_r_sql, chan_v_sql, chan_t_sql, chan_n_sql,
                           chan_conf_sql, chan_e_sql, elems_e_sql, chan_wsel_sql)

    def import_xsec(self):
        xsec_sql = ['''INSERT INTO xsec_n_data (chan_n_nxsecnum, xi, yi) VALUES''']
        xsec_part = '''\n({0}, {1}, {2})'''
        self.clear_tables('xsec_n_data')
        data = self.parser.parse_xsec()
        for xsec in data:
            nr, nodes = xsec
            for row in nodes:
                xsec_sql += [xsec_part.format(nr, *row)]

        self.batch_execute(xsec_sql)

    def import_hystruc(self):
        hystruc_params = ['geom', 'type', 'structname', 'ifporchan', 'icurvtable', 'inflonod', 'outflonod', 'inoutcont',
                          'headrefel', 'clength', 'cdiameter']
        hystruc_sql = ['INSERT INTO struct (' + ', '.join(hystruc_params) + ') VALUES']
        ratc_sql = ['''INSERT INTO rat_curves (struct_fid, hdepexc, coefq, expq, coefa, expa) VALUES''']
        repl_ratc_sql = ['''INSERT INTO repl_rat_curves (struct_fid, repdep, rqcoef, rqexp, racoef, raexp) VALUES''']
        ratt_sql = ['''INSERT INTO rat_table (struct_fid, hdepth, qtable, atable) VALUES''']
        culvert_sql = ['''INSERT INTO culvert_equations (struct_fid, typec, typeen, culvertn, ke, cubase) VALUES''']
        storm_sql = ['''INSERT INTO storm_drains (struct_fid, istormdout, stormdmax) VALUES''']

        hystruc_part = '''\n({0}, '{1}', '{2}', {3}, {4}, {5}, {6}, {7}, {8}, {9}, {10})'''
        ratc_part = '''\n({0}, {1}, {2}, {3}, {4}, {5})'''
        repl_ratc_part = '''\n({0}, {1}, {2}, {3}, {4}, {5})'''
        ratt_part = '''\n({0}, {1}, {2}, {3})'''
        culvert_part = '''\n({0}, {1}, {2}, {3}, {4}, {5})'''
        storm_part = '''\n({0}, {1}, {2})'''

        sqls = {
            'C': [ratc_sql, ratc_part],
            'R': [repl_ratc_sql, repl_ratc_part],
            'T': [ratt_sql, ratt_part],
            'F': [culvert_sql, culvert_part],
            'D': [storm_sql, storm_part]
        }

        self.clear_tables('struct', 'rat_curves', 'repl_rat_curves', 'rat_table', 'culvert_equations', 'storm_drains')
        data = self.parser.parse_hystruct()
        nodes = slice(3, 5)
        for i, hs in enumerate(data, 1):
            params = hs[:-1]
            elems = hs[-1]
            geom = self.build_linestring(params[nodes])
            typ = elems.keys()[0] if len(elems) == 1 else 'C'
            hystruc_sql += [hystruc_part.format(geom, typ, *params)]
            for char in elems.keys():
                for row in elems[char]:
                    sqls[char][0] += [sqls[char][1].format(i, *row)]

        self.batch_execute(hystruc_sql, ratc_sql, repl_ratc_sql, ratt_sql, culvert_sql, storm_sql)

    def import_street(self):
        general_sql = ['''INSERT INTO street_general (strman, istrflo, strfno, depx, widst) VALUES''']
        streets_sql = ['''INSERT INTO streets (stname) VALUES''']
        seg_sql = ['''INSERT INTO street_seg (geom, str_fid, igridn, depex, stman, elstr) VALUES''']
        elem_sql = ['''INSERT INTO street_elems (seg_fid, istdir, widr) VALUES''']

        head_part = '''\n({0}, {1}, {2}, {3}, {4})'''
        streets_part = '''\n('{0}')'''
        seg_part = '''\n({0}, {1}, {2}, {3}, {4}, {5})'''
        elem_part = '''\n({0}, {1}, {2})'''

        sqls = {
            'N': [streets_sql, streets_part],
            'S': [seg_sql, seg_part],
            'W': [elem_sql, elem_part]
        }

        self.clear_tables('street_general', 'streets', 'street_seg', 'street_elems')
        head, data = self.parser.parse_street()
        general_sql += [head_part.format(*head)]
        seg_fid = 1
        for i, n in enumerate(data, 1):
            name = n[0]
            sqls['N'][0] += [sqls['N'][1].format(name)]
            for s in n[-1]:
                gid = s[0]
                directions = []
                s_params = s[:-1]
                for w in s[-1]:
                    d = w[0]
                    directions.append(d)
                    sqls['W'][0] += [sqls['W'][1].format(seg_fid, *w)]
                geom = self.build_multilinestring(gid, directions, self.cell_size)
                sqls['S'][0] += [sqls['S'][1].format(geom, i, *s_params)]
                seg_fid += 1

        self.batch_execute(general_sql, streets_sql, seg_sql, elem_sql)

    def import_arf(self):
        cont_sql = ['''INSERT INTO cont (name, value) VALUES''']
        blocked_sql = ['''INSERT INTO blocked_areas (geom, arf) VALUES''']
        cells_sql = ['''INSERT INTO blocked_cells (area_fid, grid_fid, arf, wrf1, wrf2, wrf3, wrf4, wrf5, wrf6, wrf7, wrf8) VALUES''']

        cont_part = '''\n('arfblockmod', {0})'''
        blocked_part = '''\n({0}, 1)'''
        cells_part = '''\n({0}, {1}, {2}, {3}, {4}, {5}, {6}, {7}, {8}, {9}, {10})'''

        self.clear_tables('blocked_areas', 'blocked_cells')
        head, data = self.parser.parse_arf()
        cont_sql += [cont_part.format(*head)]
        gids = (x[0] for x in chain(data['T'], data['PB']))
        cells = self.get_centroids(gids)
        tb_wrf = ['NULL'] * 8

        for i, row in enumerate(data['T'], 1):
            gid = row[0]
            geom = self.build_square(cells[gid], self.shrink)
            blocked_sql += [blocked_part.format(geom)]
            cells_sql += [cells_part.format(i, gid, 1, *tb_wrf)]

        for row in data['PB']:
            cells_sql += [cells_part.format('NULL', *row)]

        self.batch_execute(cont_sql, blocked_sql, cells_sql)

    def import_mult(self):
        mult_sql = ['''INSERT INTO mult (wmc, wdrall, dmall, nodchansall, xnmultall, sslopemin, sslopemax, avuld50) VALUES''']
        mult_area_sql = ['''INSERT INTO mult_areas (geom, wdr, dm, nodchns, xnmult) VALUES''']
        cells_sql = ['''INSERT INTO mult_cells (area_fid, grid_fid) VALUES''']

        mult_part = '''\n({0}, {1}, {2}, {3}, {4}, {5}, {6}, {7})'''
        mult_area_part = '''\n({0}, {1}, {2}, {3}, {4})'''
        cells_part = '''\n({0}, {1})'''

        self.clear_tables('mult', 'mult_areas')
        head, data = self.parser.parse_mult()
        mult_sql += [mult_part.format(*head)]
        gids = (x[0] for x in data)
        cells = self.get_centroids(gids)
        for i, row in enumerate(data, 1):
            gid = row[0]
            geom = self.build_square(cells[gid], self.shrink)
            mult_area_sql += [mult_area_part.format(geom, *row[1:])]
            cells_sql += [cells_part.format(i, gid)]

        self.batch_execute(mult_sql, mult_area_sql, cells_sql)

    def import_sed(self):
        sed_m_sql = ['''INSERT INTO mud (va, vb, ysa, ysb, sgsm, xkx) VALUES''']
        sed_c_sql = ['''INSERT INTO sed (isedeqg, isedsizefrac, dfifty, sgrad, sgst, dryspwt, cvfg, isedsupply, isedisplay, scourdep) VALUES''']
        sgf_sql = ['''INSERT INTO sed_group_frac (fid) VALUES''']
        sed_z_sql = ['''INSERT INTO sed_groups (dist_fid, isedeqi, bedthick, cvfi) VALUES''']
        sed_p_sql = ['''INSERT INTO sed_group_frac_data (dist_fid, sediam, sedpercent) VALUES''']
        areas_d_sql = ['''INSERT INTO mud_areas (geom, debrisv) VALUES''']
        cells_d_sql = ['''INSERT INTO mud_cells (area_fid, grid_fid) VALUES''']
        areas_g_sql = ['''INSERT INTO sed_group_areas (geom, group_fid) VALUES''']
        cells_g_sql = ['''INSERT INTO sed_group_cells (area_fid, grid_fid) VALUES''']
        areas_r_sql = ['''INSERT INTO sed_rigid_areas (geom) VALUES''']
        cells_r_sql = ['''INSERT INTO sed_rigid_cells (area_fid, grid_fid) VALUES''']
        areas_s_sql = ['''INSERT INTO sed_supply_areas (geom, dist_fid, isedcfp, ased, bsed) VALUES''']
        cells_s_sql = ['''INSERT INTO sed_supply_cells (area_fid, grid_fid) VALUES''']
        sed_n_sql = ['''INSERT INTO sed_supply_frac (fid) VALUES''']
        data_n_sql = ['''INSERT INTO sed_supply_frac_data (dist_fid, ssediam, ssedpercent) VALUES''']

        sed_m_part = '\n(' + ','.join(['{} '] * 6) + ')'
        sed_c_part = '\n(' + ','.join(['{} '] * 10) + ')'
        sgf_part = '\n(' + ','.join(['{} '] * 1) + ')'
        sed_z_part = '\n(' + ','.join(['{} '] * 4) + ')'
        sed_p_part = '\n(' + ','.join(['{} '] * 3) + ')'
        areas_d_part = '\n(' + ','.join(['{} '] * 2) + ')'
        cells_d_part = '\n(' + ','.join(['{} '] * 2) + ')'
        areas_g_part = '\n(' + ','.join(['{} '] * 2) + ')'
        cells_g_part = '\n(' + ','.join(['{} '] * 2) + ')'
        areas_r_part = '\n(' + ','.join(['{} '] * 1) + ')'
        cells_r_part = '\n(' + ','.join(['{} '] * 2) + ')'
        areas_s_part = '\n(' + ','.join(['{} '] * 5) + ')'
        cells_s_part = '\n(' + ','.join(['{} '] * 2) + ')'
        sed_n_part = '\n(' + ','.join(['{} '] * 1) + ')'
        data_n_part = '\n(' + ','.join(['{} '] * 3) + ')'

        parts = [
            ['D', areas_d_sql, areas_d_part, cells_d_sql, cells_d_part],
            ['G', areas_g_sql, areas_g_part, cells_g_sql, cells_g_part],
            ['R', areas_r_sql, areas_r_part, cells_r_sql, cells_r_part]
        ]

        self.clear_tables('mud', 'mud_areas', 'mud_cells', 'sed', 'sed_groups', 'sed_group_areas', 'sed_group_cells',
                          'sed_group_frac', 'sed_group_frac_data', 'sed_rigid_areas', 'sed_rigid_cells',
                          'sed_supply_areas', 'sed_supply_cells', 'sed_supply_frac', 'sed_supply_frac_data')

        data = self.parser.parse_sed()
        gids = (x[0] for x in chain(data['D'], data['G'], data['R'], data['S']))
        cells = self.get_centroids(gids)
        for row in data['M']:
            sed_m_sql += [sed_m_part.format(*row)]
        for row in data['C']:
            erow = data['E'][0]
            if erow:
                row += erow
            else:
                row.append('NULL')
            sed_c_sql += [sed_c_part.format(*row)]
        for i, row in enumerate(data['Z'], 1):
            sgf_sql += [sgf_part.format(i)]
            sed_z_sql += [sed_z_part.format(i, *row[:-1])]
            for prow in row[-1]:
                sed_p_sql += [sed_p_part.format(i, *prow)]
        for char, asql, apart, csql, cpart in parts:
            for i, row in enumerate(data[char], 1):
                gid = row[0]
                vals = row[1:]
                geom = self.build_square(cells[gid], self.shrink)
                asql += [apart.format(geom, *vals)]
                csql += [cpart.format(i, gid)]

        for i, row in enumerate(data['S'], 1):
            gid = row[0]
            vals = row[1:-1]
            nrows = row[-1]
            geom = self.build_square(cells[gid], self.shrink)
            areas_s_sql += [areas_s_part.format(geom, i, *vals)]
            cells_s_sql += [cells_s_part.format(i, gid)]
            for ii, nrow in enumerate(nrows, 1):
                sed_n_sql += [sed_n_part.format(ii)]
                data_n_sql += [data_n_part.format(i, *nrow)]

        self.batch_execute(sed_m_sql, areas_d_sql, cells_d_sql, sed_c_sql, sgf_sql, sed_z_sql, areas_g_sql, cells_g_sql,
                           sed_p_sql, areas_r_sql, cells_r_sql, areas_s_sql, cells_s_sql, sed_n_sql, data_n_sql)

    def import_levee(self):
        lgeneral_sql = ['''INSERT INTO levee_general (raiselev, ilevfail, gfragchar, gfragprob) VALUES''']
        ldata_sql = ['''INSERT INTO levee_data (geom, grid_fid, ldir, levcrest) VALUES''']
        lfailure_sql = ['''INSERT INTO levee_failure (grid_fid, lfaildir, failevel, failtime, levbase, failwidthmax, failrate, failwidrate) VALUES''']
        lfragility_sql = ['''INSERT INTO levee_fragility (grid_fid, levfragchar, levfragprob) VALUES''']

        lgeneral_part = '''\n({0}, {1}, '{2}', {3})'''
        ldata_part = '''\n({0}, {1}, {2}, {3})'''
        lfailure_part = '''\n({0}, {1}, {2}, {3}, {4}, {5}, {6}, {7})'''
        lfragility_part = '''\n({0}, '{1}', {2})'''

        self.clear_tables('levee_general', 'levee_data', 'levee_failure', 'levee_fragility')
        head, data = self.parser.parse_levee()

        if head[2] == 'NULL':
            lgeneral_sql += [lgeneral_part.replace("'", '').format(*head)]
        else:
            lgeneral_sql += [lgeneral_part.format(*head)]

        for gid, directions in data['L']:
            for row in directions:
                ldir, levcrest = row
                geom = self.build_levee(gid, ldir, self.cell_size)
                ldata_sql += [ldata_part.format(geom, gid, ldir, levcrest)]
        for gid, directions in data['F']:
            for row in directions:
                lfailure_sql += [lfailure_part.format(gid, *row)]

        for row in data['P']:
            if row[1] == 'NULL':
                lfragility_sql += [lfragility_part.replace("'", '').format(*row)]
            else:
                lfragility_sql += [lfragility_part.format(*row)]

        self.batch_execute(lgeneral_sql, ldata_sql, lfailure_sql, lfragility_sql)

    def import_fpxsec(self):
        cont_sql = ['''INSERT INTO cont (name, value) VALUES''']
        fpxsec_sql = ['''INSERT INTO fpxsec (geom, iflo, nnxsec) VALUES''']
        cells_sql = ['''INSERT INTO fpxsec_cells (fpxsec_fid, grid_fid) VALUES''']

        cont_part = '''\n('NXPRT', {0})'''
        fpxsec_part = '''\n({0}, {1}, {2})'''
        cells_part = '''\n({0}, {1})'''

        self.clear_tables('fpxsec', 'fpxsec_cells')
        head, data = self.parser.parse_fpxsec()
        cont_sql += [cont_part.format(head)]
        for i, xs in enumerate(data, 1):
            params, gids = xs
            geom = self.build_linestring(gids)
            fpxsec_sql += [fpxsec_part.format(geom, *params)]
            for gid in gids:
                cells_sql += [cells_part.format(i, gid)]

        self.batch_execute(cont_sql, fpxsec_sql, cells_sql)

    def import_breach(self):
        glob = [
            'ibreachsedeqn', 'gbratio', 'gweircoef', 'gbreachtime', 'gzu', 'gzd', 'gzc', 'gcrestwidth', 'gcrestlength',
            'gbrbotwidmax', 'gbrtopwidmax', 'gbrbottomel', 'gd50c', 'gporc', 'guwc', 'gcnc', 'gafrc', 'gcohc', 'gunfcc',
            'gd50s', 'gpors', 'guws', 'gcns', 'gafrs', 'gcohs', 'gunfcs', 'ggrasslength', 'ggrasscond', 'ggrassvmaxp',
            'gsedconmax', 'd50df', 'gunfcdf'
        ]
        local = [
            'geom', 'ibreachdir', 'zu', 'zd', 'zc', 'crestwidth', 'crestlength', 'brbotwidmax', 'brtopwidmax',
            'brbottomel', 'weircoef', 'd50c', 'porc', 'uwc', 'cnc', 'afrc', 'cohc', 'unfcc', 'd50s', 'pors', 'uws',
            'cns', 'afrs', 'cohs', 'unfcs', 'bratio', 'grasslength', 'grasscond', 'grassvmaxp', 'sedconmax', 'd50df',
            'unfcdf', 'breachtime'
        ]
        global_sql = ['INSERT INTO breach_global (' + ', '.join(glob) + ') VALUES']
        local_sql = ['INSERT INTO breach (' + ', '.join(local) + ') VALUES']
        cells_sql = ['''INSERT INTO breach_cells (breach_fid, grid_fid) VALUES''']
        frag_sql = ['''INSERT INTO breach_fragility_curves (fragchar, prfail, prdepth) VALUES''']

        global_part = '\n(' + ','.join(['{} '] * 32) + ')'
        local_part = '\n(' + ','.join(['{} '] * 33) + ')'
        cells_part = '''\n({0}, {1})'''
        frag_part = '''\n('{0}', {1}, {2})'''

        self.clear_tables('breach_global', 'breach', 'breach_cells', 'breach_fragility_curves')
        data = self.parser.parse_breach()
        gids = (x[0] for x in data['D'])
        cells = self.get_centroids(gids)
        for row in data['G']:
            global_sql += [global_part.format(*row)]
        for i, row in enumerate(data['D'], 1):
            gid = row[0]
            geom = "AsGPB(ST_GeomFromText('{}'))".format(cells[gid])
            local_sql += [local_part.format(geom, *row[1:])]
            cells_sql += [cells_part.format(i, gid)]
        for row in data['F']:
            frag_sql += [frag_part.format(*row)]

        self.batch_execute(global_sql, local_sql, cells_sql, frag_sql)

    def import_fpfroude(self):
        fpfroude_sql = ['''INSERT INTO fpfroude (geom, froudefp) VALUES''']
        cells_sql = ['''INSERT INTO fpfroude_cells (area_fid, grid_fid) VALUES''']

        fpfroude_part = '''\n({0}, {1})'''
        cells_part = '''\n({0}, {1})'''

        self.clear_tables('fpfroude', 'fpfroude_cells')
        data = self.parser.parse_fpfroude()
        gids = (x[0] for x in data)
        cells = self.get_centroids(gids)
        for i, row in enumerate(data, 1):
            gid, froudefp = row
            geom = self.build_square(cells[gid], self.shrink)
            fpfroude_sql += [fpfroude_part.format(geom, froudefp)]
            cells_sql += [cells_part.format(i, gid)]

        self.batch_execute(fpfroude_sql, cells_sql)

    def import_swmmflo(self):
        swmmflo_sql = ['''INSERT INTO swmmflo (geom, swmm_jt, intype, swmm_length, swmm_width, swmm_height, swmm_coeff, flapgate) VALUES''']

        swmmflo_part = '''\n({0}, {1}, {2}, {3}, {4}, {5}, {6}, {7})'''

        self.clear_tables('swmmflo')
        data = self.parser.parse_swmmflo()
        gids = (x[0] for x in data)
        cells = self.get_centroids(gids)
        for row in data:
            gid = row[0]
            geom = self.build_square(cells[gid], self.shrink)
            swmmflo_sql += [swmmflo_part.format(geom, *row)]

        self.batch_execute(swmmflo_sql)

    def import_swmmflort(self):
        swmmflort_sql = ['''INSERT INTO swmmflort (grid_fid) VALUES''']
        data_sql = ['''INSERT INTO swmmflort_data (swmm_rt_fid, depth, q) VALUES''']

        swmmflort_part = '''\n({0})'''
        data_part = '''\n({0}, {1}, {2})'''

        self.clear_tables('swmmflort', 'swmmflort_data')
        data = self.parser.parse_swmmflort()
        for i, row in enumerate(data, 1):
            gid, params = row
            swmmflort_sql += [swmmflort_part.format(gid)]
            for n in params:
                data_sql += [data_part.format(i, *n)]

        self.batch_execute(swmmflort_sql, data_sql)

    def import_swmmoutf(self):
        swmmoutf_sql = ['''INSERT INTO swmmoutf (geom, name, grid_fid, outf_flo) VALUES''']

        swmmoutf_part = '''\n({0}, '{1}', {2}, {3})'''

        self.clear_tables('swmmoutf')
        data = self.parser.parse_swmmoutf()
        gids = (x[1] for x in data)
        cells = self.get_centroids(gids)
        for row in data:
            gid = row[1]
            geom = self.build_square(cells[gid], self.shrink)
            swmmoutf_sql += [swmmoutf_part.format(geom, *row)]

        self.batch_execute(swmmoutf_sql)

    def import_tolspatial(self):
        tolspatial_sql = ['''INSERT INTO tolspatial (geom, tol) VALUES''']
        cells_sql = ['''INSERT INTO tolspatial_cells (area_fid, grid_fid) VALUES''']

        tolspatial_part = '''\n({0}, {1})'''
        cells_part = '''\n({0}, {1})'''

        self.clear_tables('tolspatial', 'tolspatial_cells')
        data = self.parser.parse_tolspatial()
        gids = (x[0] for x in data)
        cells = self.get_centroids(gids)
        for i, row in enumerate(data, 1):
            gid, tol = row
            geom = self.build_square(cells[gid], self.shrink)
            tolspatial_sql += [tolspatial_part.format(geom, tol)]
            cells_sql += [cells_part.format(i, gid)]

        self.batch_execute(tolspatial_sql, cells_sql)

    def import_wsurf(self):
        wsurf_sql = ['''INSERT INTO wsurf (geom, grid_fid, wselev) VALUES''']

        wsurf_part = '''\n(AsGPB(ST_GeomFromText('{0}')), {1}, {2})'''

        self.clear_tables('wsurf')
        head, data = self.parser.parse_wsurf()
        gids = (x[0] for x in data)
        cells = self.get_centroids(gids)
        for row in data:
            gid = row[0]
            wsurf_sql += [wsurf_part.format(cells[gid], *row)]

        self.batch_execute(wsurf_sql)

    def import_wstime(self):
        wstime_sql = ['''INSERT INTO wstime (geom, grid_fid, wselev, wstime) VALUES''']

        wstime_part = '''\n(AsGPB(ST_GeomFromText('{0}')), {1}, {2}, {3})'''

        self.clear_tables('wstime')
        head, data = self.parser.parse_wstime()
        gids = (x[0] for x in data)
        cells = self.get_centroids(gids)
        for row in data:
            gid = row[0]
            wstime_sql += [wstime_part.format(cells[gid], *row)]

        self.batch_execute(wstime_sql)

    def export_cont(self, outdir):
        parser = ParseDAT()
        sql = '''SELECT name, value FROM cont;'''
        options = {o: v if v is not None else '' for o, v in self.execute(sql).fetchall()}
        cont = os.path.join(outdir, 'CONT.DAT')
        toler = os.path.join(outdir, 'TOLER.DAT')
        rline = ' {0}'
        with open(cont, 'w') as c:
            for row in parser.cont_rows:
                lst = ''
                for o in row:
                    val = options[o]
                    lst += rline.format(val)
                lst += '\n'
                if lst.isspace() is False:
                    c.write(lst)
                else:
                    pass

        with open(toler, 'w') as t:
            for row in parser.toler_rows:
                lst = ''
                for o in row:
                    val = options[o]
                    lst += rline.format(val)
                lst += '\n'
                if lst.isspace() is False:
                    t.write(lst)
                else:
                    pass

    def export_mannings_n_topo(self, outdir):
        sql = '''SELECT fid, n_value, elevation, ST_AsText(ST_Centroid(GeomFromGPB(geom))) FROM grid ORDER BY fid;'''
        records = self.execute(sql)
        mannings = os.path.join(outdir, 'MANNINGS_N.DAT')
        topo = os.path.join(outdir, 'TOPO.DAT')

        mline = '{0: >10} {1: >10}\n'
        tline = '{0: >15} {1: >15} {2: >10}\n'

        with open(mannings, 'w') as m, open(topo, 'w') as t:
            for row in records:
                fid, man, elev, geom = row
                x, y = geom.strip('POINT()').split()
                m.write(mline.format(fid, '{0:.3f}'.format(man)))
                t.write(tline.format('{0:.3f}'.format(float(x)), '{0:.3f}'.format(float(y)), '{0:.2f}'.format(elev)))

    def export_inflow(self, outdir):
        cont_sql = '''SELECT value FROM cont WHERE name = 'IDEPLT';'''
        inflow_sql = '''SELECT fid, time_series_fid, ident, inoutfc FROM inflow ORDER BY fid;'''
        inflow_cells_sql = '''SELECT inflow_fid, grid_fid FROM inflow_cells ORDER BY fid;'''
        ts_sql = '''SELECT hourdaily FROM time_series;'''
        ts_data_sql = '''SELECT time, value, value2 FROM time_series_data WHERE series_fid = {0} ORDER BY fid;'''
        reservoirs_sql = '''SELECT grid_fid, wsel FROM reservoirs ORDER BY fid;'''

        head_line = ' {0: <15} {1}'
        inf_line = '\n{0: <15} {1: <15} {2}'
        tsd_line = '\nH              {0: <15} {1: <15} {2}'
        res_line = '\nR              {0: <15} {1}'

        idplt = self.execute(cont_sql).fetchone()
        if idplt is None:
            return
        else:
            pass
        inf_cells = dict(self.execute(inflow_cells_sql).fetchall())
        hourdaily = self.execute(ts_sql).fetchone()[0]

        inflow = os.path.join(outdir, 'INFLOW.DAT')
        with open(inflow, 'w') as i:
            i.write(head_line.format(hourdaily, idplt[0]))
            for row in self.execute(inflow_sql):
                row = [x if x is not None else '' for x in row]
                fid, ts_fid, ident, inoutfc = row
                gid = inf_cells[fid]
                i.write(inf_line.format(ident, inoutfc, gid))
                series = self.execute(ts_data_sql.format(ts_fid))
                for tsd_row in series:
                    tsd_row = [x if x is not None else '' for x in tsd_row]
                    i.write(tsd_line.format(*tsd_row).rstrip())
            for res in self.execute(reservoirs_sql):
                res = [x if x is not None else '' for x in res]
                i.write(res_line.format(*res).rstrip())

    def export_outflow(self, outdir):
        outflow_sql = '''SELECT fid, time_series_fid, ident, nostacfp, qh_params_fid, qh_table_fid FROM outflow ORDER BY fid;'''
        outflow_cells_sql = '''SELECT outflow_fid, grid_fid FROM outflow_cells ORDER BY fid;'''
        outflow_chan_sql = '''SELECT outflow_fid, elem_fid FROM outflow_chan_elems ORDER BY fid;'''
        qh_sql = '''SELECT hmax, coef, exponent FROM qh_params WHERE fid = {0};'''
        qh_data_sql = '''SELECT depth, q FROM qh_table_data WHERE table_fid = {0} ORDER BY fid;'''
        ts_data_sql = '''SELECT time, value FROM time_series_data WHERE series_fid = {0} ORDER BY fid;'''
        hydchar_sql = '''SELECT hydro_fid, grid_fid FROM outflow_hydrographs ORDER BY fid;'''

        out_line = '{0}  {1}  {2}\n'
        qh_line = 'H  {0}  {1}  {2}\n'
        qh_data_line = 'T  {0}  {1}\n'
        tsd_line = 'S  {0}  {1}\n'
        hyd_line = '{0}  {1}\n'

        outflow_rows = self.execute(outflow_sql).fetchall()
        hydchar_rows = self.execute(hydchar_sql).fetchall()
        if not outflow_rows and not hydchar_rows:
            return
        else:
            pass
        out_cells = dict(self.execute(outflow_cells_sql).fetchall())
        out_chan = dict(self.execute(outflow_chan_sql).fetchall())

        outflow = os.path.join(outdir, 'OUTFLOW.DAT')
        with open(outflow, 'w') as o:
            for row in outflow_rows:
                row = [x if x is not None else '' for x in row]
                fid, ts_fid, ident, nostacfp, qh_fid, qh_tab_fid = row
                gid = out_chan[fid] if ident == 'K' else out_cells[fid]
                o.write(out_line.format(ident, gid, nostacfp))
                if qh_fid:
                    qh_params = self.execute(qh_sql.format(qh_fid)).fetchone()
                    o.write(qh_line.format(*qh_params))
                else:
                    pass
                if qh_tab_fid:
                    qh_data = self.execute(qh_data_sql.format(qh_tab_fid))
                    for qh in qh_data:
                        o.write(qh_data_line.format(*qh))
                else:
                    pass
                if ts_fid:
                    series = self.execute(ts_data_sql.format(ts_fid))
                    for tsd_row in series:
                        o.write(tsd_line.format(*tsd_row))
                else:
                    pass
            for hyd in hydchar_rows:
                o.write(hyd_line.format(*hyd))

    def export_rain(self, outdir):
        rain_sql = '''SELECT time_series_fid, irainreal, irainbuilding, tot_rainfall, rainabs, irainarf, movingstrom, rainspeed, iraindir FROM rain;'''
        rain_cells_sql = '''SELECT grid_fid, arf FROM rain_arf_cells ORDER BY fid;'''
        ts_data_sql = '''SELECT time, value FROM time_series_data WHERE series_fid = {0} ORDER BY fid;'''

        rain_line1 = '{0}  {1}\n'
        rain_line2 = '{0}   {1}  {2}  {3}\n'
        rain_line4 = '{0}   {1}\n'
        tsd_line = 'R {0:.3f}   {1:.3f}\n'
        cell_line = '{0: <10} {1}\n'

        rain_row = self.execute(rain_sql).fetchone()
        if rain_row is None:
            return
        else:
            pass
        rain = os.path.join(outdir, 'RAIN.DAT')
        with open(rain, 'w') as r:
            fid = rain_row[0]
            r.write(rain_line1.format(*rain_row[1:3]))
            r.write(rain_line2.format(*rain_row[3:7]))
            for row in self.execute(ts_data_sql.format(fid)):
                r.write(tsd_line.format(*row))
            if rain_row[-1] is not None:
                r.write(rain_line4.format(*rain_row[-2:]))
            else:
                pass
            for row in self.execute(rain_cells_sql):
                r.write(cell_line.format(*row))

    def export_infil(self, outdir):
        infil_sql = '''SELECT * FROM infil;'''
        infil_r_sql = '''SELECT hydcx, hydcxfinal, soildepthcx FROM infil_chan_seg ORDER BY chan_seg_fid, fid;'''
        iarea_green_sql = '''SELECT hydc, soils, dtheta, abstrinf, rtimpf, soil_depth FROM infil_areas_green WHERE fid = {0};'''
        icell_green_sql = '''SELECT grid_fid, infil_area_fid FROM infil_cells_green ORDER BY grid_fid;'''
        iarea_scs_sql = '''SELECT scscn FROM infil_areas_scs WHERE fid = {0};'''
        icell_scs_sql = '''SELECT grid_fid, infil_area_fid FROM infil_cells_scs ORDER BY grid_fid;'''
        iarea_horton_sql = '''SELECT fhorti, fhortf, deca FROM infil_areas_horton WHERE fid = {0};'''
        icell_horton_sql = '''SELECT grid_fid, infil_area_fid FROM infil_cells_horton ORDER BY grid_fid;'''
        iarea_chan_sql = '''SELECT hydconch FROM infil_areas_chan WHERE fid = {0};'''
        ielem_chan_sql = '''SELECT grid_fid, infil_area_fid FROM infil_chan_elems ORDER BY grid_fid;'''

        line1 = '{0}'
        line2 = '\n' + '  {}' * 6
        line3 = '\n' + '  {}' * 3
        line4 = '\n{0}'
        line4ab = '\nR  {0}  {1}  {2}'
        line5 = '\n{0}  {1}'
        line6 = '\n' + 'F' + '  {}' * 7
        line7 = '\nS  {0}  {1}'
        line8 = '\nC  {0}  {1}'
        line9 = '\nI  {0}  {1}  {2}'
        line10 = '\nH  {0}  {1}  {2}  {3}'

        infil_row = self.execute(infil_sql).fetchone()
        if infil_row is None:
            return
        else:
            pass
        infil = os.path.join(outdir, 'INFIL.DAT')
        with open(infil, 'w') as i:
            gen = [x if x is not None else '' for x in infil_row[1:]]
            v1, v2, v3, v4, v5, v9 = gen[0], gen[1:7], gen[7:10], gen[10:11], gen[11:13], gen[13:]
            i.write(line1.format(v1))
            for val, line in izip([v2, v3, v4], [line2, line3, line4]):
                if any(val) is True:
                    i.write(line.format(*val))
                else:
                    pass
            for row in self.execute(infil_r_sql):
                row = [x if x is not None else '' for x in row]
                i.write(line4ab.format(*row))
            if any(v5) is True:
                i.write(line5.format(*v5))
            else:
                pass
            for gid, iid in self.execute(icell_green_sql):
                for row in self.execute(iarea_green_sql.format(iid)):
                    i.write(line6.format(gid, *row))
            for gid, iid in self.execute(icell_scs_sql):
                for row in self.execute(iarea_scs_sql.format(iid)):
                    i.write(line7.format(gid, *row))
            for gid, iid in self.execute(ielem_chan_sql):
                for row in self.execute(iarea_chan_sql.format(iid)):
                    i.write(line8.format(gid, *row))
            if any(v9) is True:
                i.write(line9.format(*v9))
            else:
                pass
            for gid, iid in self.execute(icell_horton_sql):
                for row in self.execute(iarea_horton_sql.format(iid)):
                    i.write(line10.format(gid, *row))

    def export_evapor(self, outdir):
        evapor_sql = '''SELECT ievapmonth, iday, clocktime FROM evapor;'''
        evapor_month_sql = '''SELECT month, monthly_evap FROM evapor_monthly ORDER BY fid;'''
        evapor_hour_sql = '''SELECT hourly_evap FROM evapor_hourly WHERE month = '{0}' ORDER BY fid;'''

        head = '{0}   {1}   {2:.2f}\n'
        monthly = '  {0}  {1:.2f}\n'
        hourly = '    {0:.4f}\n'

        evapor_row = self.execute(evapor_sql).fetchone()
        if evapor_row is None:
            return
        else:
            pass
        evapor = os.path.join(outdir, 'EVAPOR.DAT')
        with open(evapor, 'w') as e:
            e.write(head.format(*evapor_row))
            for mrow in self.execute(evapor_month_sql):
                month = mrow[0]
                e.write(monthly.format(*mrow))
                for hrow in self.execute(evapor_hour_sql.format(month)):
                    e.write(hourly.format(*hrow))

    def export_chan(self, outdir):
        chan_sql = '''SELECT fid, depinitial, froudc, roughadj, isedn FROM chan ORDER BY fid;'''
        chan_elems_sql = '''SELECT fid, rbankgrid, fcn, xlen, type FROM chan_elems WHERE seg_fid = {0} ORDER BY nr_in_seg;'''

        chan_r_sql = '''SELECT elem_fid, bankell, bankelr, fcw, fcd FROM chan_r WHERE elem_fid = {0};'''
        chan_v_sql = '''SELECT elem_fid, bankell, bankelr, fcd, a1, a2, b1, b2, c1, c2, excdep, a11, a22, b11, b22, c11, c22 FROM chan_v WHERE elem_fid = {0};'''
        chan_t_sql = '''SELECT elem_fid, bankell, bankelr, fcw, fcd, zl, zr FROM chan_t WHERE elem_fid = {0};'''
        chan_n_sql = '''SELECT elem_fid, nxsecnum FROM chan_n WHERE elem_fid = {0};'''

        chan_wsel_sql = '''SELECT istart, wselstart, iend, wselend FROM chan_wsel ORDER BY fid;'''
        chan_conf_sql = '''SELECT chan_elem_fid FROM chan_confluences ORDER BY fid;'''
        chan_e_sql = '''SELECT chan_elem_fid FROM noexchange_chan_elems ORDER BY fid;'''

        segment = '   {0:.2f}   {1:.2f}   {2:.2f}   {3}\n'
        chan_r = 'R' + '  {}' * 7 + '\n'
        chan_v = 'V' + '  {}' * 19 + '\n'
        chan_t = 'T' + '  {}' * 9 + '\n'
        chan_n = 'N' + '  {}' * 4 + '\n'
        chanbank = ' {0: <10} {1}\n'
        wsel = '{0} {1:.2f}\n'
        conf = ' C {0}  {1}\n'
        chan_e = ' E {0}\n'

        sqls = {
            'R': [chan_r_sql, chan_r, 3, 6],
            'V': [chan_v_sql, chan_v, 3, 5],
            'T': [chan_t_sql, chan_t, 3, 6],
            'N': [chan_n_sql, chan_n, 1, 2]
        }

        chan_rows = self.execute(chan_sql).fetchall()
        if not chan_rows:
            return
        else:
            pass
        chan = os.path.join(outdir, 'CHAN.DAT')
        bank = os.path.join(outdir, 'CHANBANK.DAT')

        with open(chan, 'w') as c, open(bank, 'w') as b:
            for row in chan_rows:
                row = [x if x is not None else '' for x in row]
                fid = row[0]
                c.write(segment.format(*row[1:]))
                for elems in self.execute(chan_elems_sql.format(fid)):
                    elems = [x if x is not None else '' for x in elems]
                    eid, rbank, fcn, xlen, typ = elems
                    sql, line, fcn_idx, xlen_idx = sqls[typ]
                    res = [x if x is not None else '' for x in self.execute(sql.format(eid)).fetchone()]
                    res.insert(fcn_idx, fcn)
                    res.insert(xlen_idx, xlen)
                    c.write(line.format(*res))
                    b.write(chanbank.format(eid, rbank))

            for row in self.execute(chan_wsel_sql):
                c.write(wsel.format(*row[:2]))
                c.write(wsel.format(*row[2:]))

            pairs = []
            for row in self.execute(chan_conf_sql):
                chan_elem = row[0]
                if not pairs:
                    pairs.append(chan_elem)
                else:
                    pairs.append(chan_elem)
                    c.write(conf.format(*pairs))
                    del pairs[:]

            for row in self.execute(chan_e_sql):
                c.write(chan_e.format(row[0]))

    def export_xsec(self, outdir):
        chan_n_sql = '''SELECT nxsecnum, xsecname FROM chan_n ORDER BY nxsecnum;'''
        xsec_sql = '''SELECT xi, yi FROM xsec_n_data WHERE chan_n_nxsecnum = {0} ORDER BY fid;'''

        xsec_line = '''X     {0}  {1}\n'''
        pkt_line = ''' {0:<10} {1: >10}\n'''
        nr = '{0:.2f}'

        chan_n = self.execute(chan_n_sql).fetchall()
        if not chan_n:
            return
        else:
            pass
        xsec = os.path.join(outdir, 'XSEC.DAT')
        with open(xsec, 'w') as x:
            for nxecnum, xsecname in chan_n:
                x.write(xsec_line.format(nxecnum, xsecname))
                for xi, yi in self.execute(xsec_sql.format(nxecnum)):
                    x.write(pkt_line.format(nr.format(xi), nr.format(yi)))

    def export_hystruc(self, outdir):
        hystruct_sql = '''SELECT * FROM struct ORDER BY fid;'''
        ratc_sql = '''SELECT * FROM rat_curves WHERE struct_fid = {0} ORDER BY fid;'''
        repl_ratc_sql = '''SELECT * FROM repl_rat_curves WHERE struct_fid = {0} ORDER BY fid;'''
        ratt_sql = '''SELECT * FROM rat_table WHERE struct_fid = {0} ORDER BY fid;'''
        culvert_sql = '''SELECT * FROM culvert_equations WHERE struct_fid = {0} ORDER BY fid;'''
        storm_sql = '''SELECT * FROM storm_drains WHERE struct_fid = {0} ORDER BY fid;'''

        line1 = 'S' + '  {}' * 9 + '\n'
        line2 = 'C' + '  {}' * 5 + '\n'
        line3 = 'R' + '  {}' * 5 + '\n'
        line4 = 'T' + '  {}' * 3 + '\n'
        line5 = 'F' + '  {}' * 5 + '\n'
        line6 = 'D' + '  {}' * 2 + '\n'

        pairs = [
            [ratc_sql, line2],
            [repl_ratc_sql, line3],
            [ratt_sql, line4],
            [culvert_sql, line5],
            [storm_sql, line6]
            ]

        hystruc_rows = self.execute(hystruct_sql).fetchall()
        if not hystruc_rows:
            return
        else:
            pass
        hystruc = os.path.join(outdir, 'HYSTRUC.DAT')
        with open(hystruc, 'w') as h:
            for stru in hystruc_rows:
                fid = stru[0]
                vals = [x if x is not None else '' for x in stru[2:-2]]
                h.write(line1.format(*vals))
                for qry, line in pairs:
                    for row in self.execute(qry.format(fid)):
                        subvals = [x if x is not None else '' for x in row[2:]]
                        h.write(line.format(*subvals))

    def export_street(self, outdir):
        street_gen_sql = '''SELECT * FROM street_general ORDER BY fid;'''
        streets_sql = '''SELECT stname FROM streets ORDER BY fid;'''
        streets_seg_sql = '''SELECT igridn, depex, stman, elstr FROM street_seg WHERE str_fid = {0} ORDER BY fid;'''
        streets_elem_sql = '''SELECT istdir, widr FROM street_elems WHERE seg_fid = {0} ORDER BY fid;'''

        line1 = '  {}' * 5 + '\n'
        line2 = ' N {}\n'
        line3 = ' S' + '  {}' * 4 + '\n'
        line4 = ' W' + '  {}' * 2 + '\n'

        head = self.execute(street_gen_sql).fetchone()
        if head is None:
            return
        else:
            pass
        street = os.path.join(outdir, 'STREET.DAT')
        with open(street, 'w') as s:
            s.write(line1.format(*head[1:]))
            seg_fid = 1
            for i, sts in enumerate(self.execute(streets_sql), 1):
                s.write(line2.format(*sts))
                for seg in self.execute(streets_seg_sql.format(i)):
                    s.write(line3.format(*seg))
                    for elem in self.execute(streets_elem_sql.format(seg_fid)):
                        s.write(line4.format(*elem))
                    seg_fid += 1

    def export_arf(self, outdir):
        cont_sql = '''SELECT name, value FROM cont WHERE name = 'arfblockmod';'''
        tbc_sql = '''SELECT grid_fid FROM blocked_cells WHERE arf = 1 ORDER BY grid_fid;'''
        pbc_sql = '''SELECT grid_fid, arf, wrf1, wrf2, wrf3, wrf4, wrf5, wrf6, wrf7, wrf8 FROM blocked_cells WHERE arf < 1 ORDER BY grid_fid;'''

        line1 = 'S  {}\n'
        line2 = ' T   {}\n'
        line3 = '   {}' * 10 + '\n'

        option = self.execute(cont_sql).fetchone()
        if option is None:
            return
        else:
            pass
        arf = os.path.join(outdir, 'ARF.DAT')
        with open(arf, 'w') as a:
            head = option[-1]
            if head is not None:
                a.write(line1.format(head))
            else:
                pass
            for row in self.execute(tbc_sql):
                a.write(line2.format(*row))
            for row in self.execute(pbc_sql):
                row = [x if x is not None else '' for x in row]
                a.write(line3.format(*row))

    def export_mult(self, outdir):
        mult_sql = '''SELECT * FROM mult;'''
        mult_cell_sql = '''SELECT grid_fid, area_fid FROM mult_cells ORDER BY grid_fid;'''
        mult_area_sql = '''SELECT wdr, dm, nodchns, xnmult FROM mult_areas WHERE fid = {0};'''

        line1 = ' {}' * 8 + '\n'
        line2 = ' {}' * 5 + '\n'

        head = self.execute(mult_sql).fetchone()
        if head is None:
            return
        else:
            pass
        mult = os.path.join(outdir, 'MULT.DAT')
        with open(mult, 'w') as m:
            m.write(line1.format(*head[1:]).replace('None', ''))
            for gid, aid in self.execute(mult_cell_sql):
                for row in self.execute(mult_area_sql.format(aid)):
                    vals = [x if x is not None else '' for x in row]
                    m.write(line2.format(gid, *vals))

    def export_sed(self, outdir):
        sed_m_sql = '''SELECT va, vb, ysa, ysb, sgsm, xkx FROM mud ORDER BY fid;'''
        sed_ce_sql = '''SELECT isedeqg, isedsizefrac, dfifty, sgrad, sgst, dryspwt, cvfg, isedsupply, isedisplay, scourdep FROM sed ORDER BY fid;'''
        sed_z_sql = '''SELECT dist_fid, isedeqi, bedthick, cvfi FROM sed_groups ORDER BY dist_fid;'''
        sed_p_sql = '''SELECT sediam, sedpercent FROM sed_group_frac_data WHERE dist_fid = {0} ORDER BY sedpercent;'''
        areas_d_sql = '''SELECT fid, debrisv FROM mud_areas ORDER BY fid;'''
        cells_d_sql = '''SELECT grid_fid FROM mud_cells WHERE area_fid = {0} ORDER BY grid_fid;'''
        cells_r_sql = '''SELECT grid_fid FROM sed_rigid_cells ORDER BY grid_fid;'''
        areas_s_sql = '''SELECT fid, dist_fid, isedcfp, ased, bsed FROM sed_supply_areas ORDER BY dist_fid;'''
        cells_s_sql = '''SELECT grid_fid FROM sed_supply_cells WHERE area_fid = {0};'''
        data_n_sql = '''SELECT ssediam, ssedpercent FROM sed_supply_frac_data WHERE dist_fid = {0} ORDER BY ssedpercent;'''
        areas_g_sql = '''SELECT fid, group_fid FROM sed_group_areas ORDER BY fid;'''
        cells_g_sql = '''SELECT grid_fid FROM sed_group_cells WHERE area_fid = {0} ORDER BY grid_fid;'''

        line1 = 'M  {0}  {1}  {2}  {3}  {4}  {5}\n'
        line2 = 'C  {0}  {1}  {2}  {3}  {4}  {5}  {6}\n'
        line3 = 'Z  {0}  {1}  {2}\n'
        line4 = 'P  {0}  {1}\n'
        line5 = 'D  {0}  {1}\n'
        line6 = 'E  {0}\n'
        line7 = 'R  {0}\n'
        line8 = 'S  {0}  {1}  {2}  {3}\n'
        line9 = 'N  {0}  {1}\n'
        line10 = 'G  {0}  {1}\n'

        m_data = self.execute(sed_m_sql).fetchone()
        ce_data = self.execute(sed_ce_sql).fetchone()
        if m_data is None and ce_data is None:
            return
        else:
            pass
        sed = os.path.join(outdir, 'SED.DAT')
        with open(sed, 'w') as s:
            if m_data is not None:
                s.write(line1.format(*m_data))
                e_data = None
            else:
                e_data = ce_data[-1]
                s.write(line2.format(*ce_data[:-1]))
            for row in self.execute(sed_z_sql):
                dist_fid = row[0]
                s.write(line3.format(*row[1:]))
                for prow in self.execute(sed_p_sql.format(dist_fid)):
                    s.write(line4.format(*prow))
            for aid, debrisv in self.execute(areas_d_sql):
                gid = self.execute(cells_d_sql.format(aid)).fetchone()[0]
                s.write(line5.format(gid, debrisv))
            if e_data is not None:
                s.write(line6.format(e_data))
            else:
                pass
            for row in self.execute(cells_r_sql):
                s.write(line7.format(*row))
            for row in self.execute(areas_s_sql):
                aid = row[0]
                dist_fid = row[1]
                gid = self.execute(cells_s_sql.format(aid)).fetchone()[0]
                s.write(line8.format(gid, *row[1:]))
                for nrow in self.execute(data_n_sql.format((dist_fid))):
                    s.write(line9.format(*nrow))
            for aid, group_fid in self.execute(areas_g_sql):
                gid = self.execute(cells_g_sql.format(aid)).fetchone()[0]
                s.write(line10.format(gid, group_fid))

    def export_levee(self, outdir):
        levee_gen_sql = '''SELECT raiselev, ilevfail, gfragchar, gfragprob FROM levee_general;'''
        levee_data_sql = '''SELECT grid_fid, ldir, levcrest FROM levee_data ORDER BY grid_fid, fid;'''
        levee_fail_sql = '''SELECT * FROM levee_failure ORDER BY grid_fid, fid;'''
        levee_frag_sql = '''SELECT grid_fid, levfragchar, levfragprob FROM levee_fragility ORDER BY grid_fid;'''

        line1 = '{0}  {1}\n'
        line2 = 'L  {0}\n'
        line3 = 'D  {0}  {1}\n'
        line4 = 'F  {0}\n'
        line5 = 'W  {0}  {1}  {2}  {3}  {4}  {5}\n'
        line6 = 'C  {0}  {1}\n'
        line7 = 'P  {0}  {1}  {2}\n'

        general = self.execute(levee_gen_sql).fetchone()
        if general is None:
            return
        else:
            pass
        head = general[:2]
        glob_frag = general[2:]
        levee = os.path.join(outdir, 'LEVEE.DAT')
        with open(levee, 'w') as l:
            l.write(line1.format(*head))
            levee_rows = groupby(self.execute(levee_data_sql), key=itemgetter(0))
            for gid, directions in levee_rows:
                l.write(line2.format(gid))
                for row in directions:
                    l.write(line3.format(*row[1:]))
            fail_rows = groupby(self.execute(levee_fail_sql), key=itemgetter(1))
            for gid, directions in fail_rows:
                l.write(line4.format(gid))
                for row in directions:
                    l.write(line5.format(*row[2:]))
            if None not in glob_frag:
                l.write(line6.format(*glob_frag))
            else:
                pass
            for row in self.execute(levee_frag_sql):
                l.write(line7.format(*row))

    def export_fpxsec(self, outdir):
        cont_sql = '''SELECT name, value FROM cont WHERE name = 'NXPRT';'''
        fpxsec_sql = '''SELECT fid, iflo, nnxsec FROM fpxsec ORDER BY fid;'''
        cell_sql = '''SELECT grid_fid FROM fpxsec_cells WHERE fpxsec_fid = {0} ORDER BY grid_fid;'''

        line1 = 'P  {}\n'
        line2 = 'X {0} {1} {2}\n'

        option = self.execute(cont_sql).fetchone()
        if option is None:
            return
        else:
            pass
        fpxsec = os.path.join(outdir, 'FPXSEC.DAT')
        with open(fpxsec, 'w') as f:
            head = option[-1]
            f.write(line1.format(head))

            for row in self.execute(fpxsec_sql):
                fid, iflo, nnxsec = row
                grids = self.execute(cell_sql.format(fid))
                grids_txt = ' '.join(['{}'.format(x[0]) for x in grids])
                f.write(line2.format(iflo, nnxsec, grids_txt))

    def export_breach(self, outdir):
        global_sql = '''SELECT * FROM breach_global ORDER BY fid;'''
        local_sql = '''SELECT * FROM breach ORDER BY fid;'''
        cells_sql = '''SELECT grid_fid FROM breach_cells WHERE breach_fid = {0};'''
        frag_sql = '''SELECT fragchar, prfail, prdepth FROM breach_fragility_curves ORDER BY fid;'''

        b1, g1, g2, g3, g4 = slice(1, 5), slice(5, 13), slice(13, 20), slice(20, 27), slice(27, 33)
        b2, d1, d2, d3, d4 = slice(0, 2), slice(2, 11), slice(11, 18), slice(18, 25), slice(25, 33)

        bline = 'B{0} {1}\n'
        line_1 = '{0}1 {1}\n'
        line_2 = '{0}2 {1}\n'
        line_3 = '{0}3 {1}\n'
        line_4 = '{0}4 {1}\n'
        fline = 'F {0} {1} {2}\n'

        parts = [
            [g1, d1, line_1],
            [g2, d2, line_2],
            [g3, d3, line_3],
            [g4, d4, line_4]
        ]

        global_rows = self.execute(global_sql).fetchall()
        local_rows = self.execute(local_sql).fetchall()
        if not global_rows and not local_rows:
            return
        else:
            pass
        breach = os.path.join(outdir, 'BREACH.DAT')
        with open(breach, 'w') as b:
            c = 1
            for row in global_rows:
                row_slice = [str(x) if x is not None else '' for x in row[b1]]
                b.write(bline.format(c, ' '.join(row_slice)))
                for gslice, dslice, line in parts:
                    row_slice = [str(x) if x is not None else '' for x in row[gslice]]
                    if any(row_slice) is True:
                        b.write(line.format('G', '  '.join(row_slice)))
                    else:
                        pass
                c += 1
            for row in local_rows:
                fid = row[0]
                gid = self.execute(cells_sql.format(fid)).fetchone()[0]
                row_slice = [str(x) if x is not None else '' for x in row[b2]]
                row_slice[0] = str(gid)
                b.write(bline.format(c, ' '.join(row_slice)))
                for gslice, dslice, line in parts:
                    row_slice = [str(x) if x is not None else '' for x in row[dslice]]
                    if any(row_slice) is True:
                        b.write(line.format('D', '  '.join(row_slice)))
                    else:
                        pass
                c += 1
            for row in self.execute(frag_sql):
                b.write(fline.format(*row))

    def export_fpfroude(self, outdir):
        fpfroude_sql = '''SELECT fid, froudefp FROM fpfroude ORDER BY fid;'''
        cell_sql = '''SELECT grid_fid FROM fpfroude_cells WHERE area_fid = {0} ORDER BY grid_fid;'''

        line1 = 'F {0} {1}\n'

        fpfroude_rows = self.execute(fpfroude_sql).fetchall()
        if not fpfroude_rows:
            return
        else:
            pass
        fpfroude = os.path.join(outdir, 'FPFROUDE.DAT')
        with open(fpfroude, 'w') as f:
            for fid, froudefp in fpfroude_rows:
                gid = self.execute(cell_sql.format(fid)).fetchone()[0]
                f.write(line1.format(gid, froudefp))

    def export_swmmflo(self, outdir):
        swmmflo_sql = '''SELECT swmm_jt, intype, swmm_length, swmm_width, swmm_height, swmm_coeff, flapgate FROM swmmflo ORDER BY fid;'''

        line1 = 'D  {0} {1} {2} {3} {4} {5} {6}\n'

        swmmflo_rows = self.execute(swmmflo_sql).fetchall()
        if not swmmflo_rows:
            return
        else:
            pass
        swmmflo = os.path.join(outdir, 'SWMMFLO.DAT')
        with open(swmmflo, 'w') as s:
            for row in swmmflo_rows:
                s.write(line1.format(*row))

    def export_swmmflort(self, outdir):
        swmmflort_sql = '''SELECT fid, grid_fid FROM swmmflort ORDER BY grid_fid;'''
        data_sql = '''SELECT depth, q FROM swmmflort_data WHERE swmm_rt_fid = {0} ORDER BY depth;'''

        line1 = 'D {0}\n'
        line2 = 'N {0}  {1}\n'

        swmmflort_rows = self.execute(swmmflort_sql).fetchall()
        if not swmmflort_rows:
            return
        else:
            pass
        swmmflort = os.path.join(outdir, 'SWMMFLORT.DAT')
        with open(swmmflort, 'w') as s:
            for fid, gid in swmmflort_rows:
                s.write(line1.format(gid))
                for row in self.execute(data_sql.format(fid)):
                    s.write(line2.format(*row))

    def export_swmmoutf(self, outdir):
        swmmoutf_sql = '''SELECT name, grid_fid, outf_flo FROM swmmoutf ORDER BY fid;'''

        line1 = '{0}  {1}  {2}\n'

        swmmoutf_rows = self.execute(swmmoutf_sql).fetchall()
        if not swmmoutf_rows:
            return
        else:
            pass
        swmmoutf = os.path.join(outdir, 'SWMMOUTF.DAT')
        with open(swmmoutf, 'w') as s:
            for row in swmmoutf_rows:
                s.write(line1.format(*row))

    def export_tolspatial(self, outdir):
        tolspatial_sql = '''SELECT fid, tol FROM tolspatial ORDER BY fid;'''
        cell_sql = '''SELECT grid_fid FROM tolspatial_cells WHERE area_fid = {0} ORDER BY grid_fid;'''

        line1 = '{0}  {1}\n'

        tolspatial_rows = self.execute(tolspatial_sql).fetchall()
        if not tolspatial_rows:
            return
        else:
            pass
        tolspatial = os.path.join(outdir, 'TOLSPATIAL.DAT')
        with open(tolspatial, 'w') as t:
            for fid, tol in tolspatial_rows:
                for row in self.execute(cell_sql.format(fid)):
                    gid = row[0]
                    t.write(line1.format(gid, tol))

    def export_wsurf(self, outdir):
        count_sql = '''SELECT COUNT(fid) FROM wsurf;'''
        wsurf_sql = '''SELECT grid_fid, wselev FROM wsurf ORDER BY fid;'''

        line1 = '{0}\n'
        line2 = '{0}  {1}\n'

        wsurf_rows = self.execute(wsurf_sql).fetchall()
        if not wsurf_rows:
            return
        else:
            pass
        wsurf = os.path.join(outdir, 'WSURF.DAT')
        with open(wsurf, 'w') as w:
            count = self.execute(count_sql).fetchone()[0]
            w.write(line1.format(count))
            for row in wsurf_rows:
                w.write(line2.format(*row))

    def export_wstime(self, outdir):
        count_sql = '''SELECT COUNT(fid) FROM wstime;'''
        wstime_sql = '''SELECT grid_fid, wselev, wstime FROM wstime ORDER BY fid;'''

        line1 = '{0}\n'
        line2 = '{0}  {1}  {2}\n'

        wstime_rows = self.execute(wstime_sql).fetchall()
        if not wstime_rows:
            return
        else:
            pass
        wstime = os.path.join(outdir, 'WSTIME.DAT')
        with open(wstime, 'w') as w:
            count = self.execute(count_sql).fetchone()[0]
            w.write(line1.format(count))
            for row in wstime_rows:
                w.write(line2.format(*row))
