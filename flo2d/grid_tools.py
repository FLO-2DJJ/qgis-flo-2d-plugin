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
import math
from qgis.core import *


def build_grid(boundary, cellsize):
    half_size = cellsize * 0.5
    biter = boundary.getFeatures()
    feature = next(biter)
    fgeom = feature.geometry()
    bbox = fgeom.boundingBox()
    xmin = math.floor(bbox.xMinimum())
    xmax = math.ceil(bbox.xMaximum())
    ymax = math.ceil(bbox.yMaximum())
    ymin = math.floor(bbox.yMinimum())
    cols = int(math.ceil(abs(xmax - xmin) / cellsize))
    rows = int(math.ceil(abs(ymax - ymin) / cellsize))
    x = xmin + half_size
    y = ymax - half_size
    for col in xrange(cols):
        y_tmp = y
        for row in xrange(rows):
            if fgeom.contains(QgsPoint(x, y_tmp)):
                poly = (
                    x - half_size, y_tmp - half_size,
                    x + half_size, y_tmp - half_size,
                    x + half_size, y_tmp + half_size,
                    x - half_size, y_tmp + half_size,
                    x - half_size, y_tmp - half_size
                )
                yield poly
            else:
                pass
            y_tmp -= cellsize
        x += cellsize


def square_grid(gutils, boundary):
    del_qry = 'DELETE FROM grid;'
    cellsize = gutils.execute('''SELECT value FROM cont WHERE name = "CELLSIZE";''').fetchone()[0]
    update_cellsize = 'UPDATE user_model_boundary SET cell_size = ?;'
    insert_qry = '''INSERT INTO grid (geom) VALUES (AsGPB(ST_GeomFromText('POLYGON(({} {}, {} {}, {} {}, {} {}, {} {}))')));'''
    gutils.execute(update_cellsize, (cellsize,))
    cellsize = float(cellsize)
    polygons = build_grid(boundary, cellsize)
    cur = gutils.con.cursor()
    cur.execute(del_qry)
    c = 0
    for poly in polygons:
        cur.execute(insert_qry.format(*poly))
        c += 1
    gutils.con.commit()
    return c


def roughness2grid(grid, roughness, column_name):
    roughness_polys = roughness.selectedFeatures() if roughness.selectedFeatureCount() > 0 else roughness.getFeatures()
    allfeatures = {feature.id(): feature for feature in roughness_polys}
    index = QgsSpatialIndex()
    map(index.insertFeature, allfeatures.itervalues())
    with edit(grid):
        for feat in grid.getFeatures():
            geom = feat.geometry()
            centroid = geom.centroid()
            fids = index.intersects(centroid.boundingBox())
            for fid in fids:
                f = allfeatures[fid]
                isin = f.geometry().contains(centroid)
                if isin is True:
                    grid.changeAttributeValue(feat.id(), 4, f.attribute(column_name))
                else:
                    pass


def calculate_arfwrf(grid, areas):
    sides = (
        (lambda x, y, square_half, octa_half: (x - octa_half, y + square_half, x + octa_half, y + square_half)),
        (lambda x, y, square_half, octa_half: (x + square_half, y + octa_half, x + square_half, y - octa_half)),
        (lambda x, y, square_half, octa_half: (x + octa_half, y - square_half, x - octa_half, y - square_half)),
        (lambda x, y, square_half, octa_half: (x - square_half, y - octa_half, x - square_half, y + octa_half)),
        (lambda x, y, square_half, octa_half: (x + octa_half, y + square_half, x + square_half, y + octa_half)),
        (lambda x, y, square_half, octa_half: (x + square_half, y - octa_half, x + octa_half, y - square_half)),
        (lambda x, y, square_half, octa_half: (x - octa_half, y - square_half, x - square_half, y - octa_half)),
        (lambda x, y, square_half, octa_half: (x - square_half, y + octa_half, x - octa_half, y + square_half))
    )
    area_polys = areas.selectedFeatures() if areas.selectedFeatureCount() > 0 else areas.getFeatures()
    allfeatures = {feature.id(): feature for feature in area_polys}
    index = QgsSpatialIndex()
    map(index.insertFeature, allfeatures.itervalues())
    features = grid.getFeatures()
    first = next(features)
    grid_area = first.geometry().area()
    grid_side = math.sqrt(grid_area)
    octagon_side = grid_side / 2.414
    half_square = grid_side * 0.5
    half_octagon = octagon_side * 0.5
    empty_wrf = (None,) * 8
    for feat in grid.getFeatures():
        geom = feat.geometry()
        fids = index.intersects(geom.boundingBox())
        for fid in fids:
            f = allfeatures[fid]
            fgeom = f.geometry()
            inter = fgeom.intersects(geom)
            if inter is True:
                areas_intersection = fgeom.intersection(geom)
                arf = round(areas_intersection.area() / grid_area, 2)
                centroid = geom.centroid()
                centroid_wkt = centroid.exportToWkt()
                if arf > 0.95:
                    yield (centroid_wkt, feat.id(), 1) + empty_wrf
                    continue
                else:
                    pass
                grid_center = centroid.asPoint()
                wrf_sides = (f(grid_center.x(), grid_center.y(), half_square, half_octagon) for f in sides)
                wrf_geoms = (QgsGeometry.fromPolyline([QgsPoint(x1, y1), QgsPoint(x2, y2)]) for x1, y1, x2, y2 in wrf_sides)
                wrf = (round(line.intersection(fgeom).length() / octagon_side, 2) for line in wrf_geoms)
                yield (centroid_wkt, feat.id(), arf) + tuple(wrf)
            else:
                pass


def evaluate_arfwrf(gutils, grid, areas):
    del_cells = 'DELETE FROM blocked_cells;'
    qry_cells = '''INSERT INTO blocked_cells (geom, area_fid, grid_fid, arf, wrf1, wrf2, wrf3, wrf4, wrf5, wrf6, wrf7, wrf8) VALUES (AsGPB(ST_GeomFromText('{}')),?,?,?,?,?,?,?,?,?,?,?);'''
    gutils.execute(del_cells)
    cur = gutils.con.cursor()
    for i, row in enumerate(calculate_arfwrf(grid, areas), 1):
        point = row[0]
        gpb_qry = qry_cells.format(point)
        cur.execute(gpb_qry, (i,) + row[1:])
    gutils.con.commit()
