# -*- coding: utf-8 -*-

# FLO-2D Preprocessor tools for QGIS
# Copyright © 2021 Lutra Consulting for FLO-2D

# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version

# QgsMapToolIdentify required those functions to be self
# pylint: disable=no-self-use

import os
from qgis.PyQt.QtCore import pyqtSignal
from qgis.PyQt.QtGui import QCursor, QPixmap
from qgis.gui import QgsMapToolIdentify


class GridInfoTool(QgsMapToolIdentify):

    grid_elem_picked = pyqtSignal(int)

    def __init__(self, uc, canvas, lyrs):
        self.uc = uc
        self.canvas = canvas
        self.lyrs = lyrs
        self.grid = None

        QgsMapToolIdentify.__init__(self, self.canvas)

    def canvasPressEvent(self, e):
        pass

    def canvasReleaseEvent(self, e):
        try:
            res = self.identify(e.x(), e.y(), [self.grid], QgsMapToolIdentify.ActiveLayer)
            if res:
                self.grid_elem_picked.emit(res[0].mFeature.id())
            else:
                self.grid_elem_picked.emit(-1)
        except Exception:
            self.uc.bar_error("ERROR 100721.1942: is the grid defined?") 

    def activate(self):
        self.canvas.setCursor(QCursor(QPixmap(os.path.join(os.path.dirname(__file__), "img/info_tool_icon.svg"))))

    def deactivate(self):
        pass

    def isZoomTool(self):
        return False

    def isTransient(self):
        return False

    def isEditTool(self):
        return False
