"""EQuIS Data Loader QGIS plugin."""


def classFactory(iface):
    from .plugin import EquisLoaderPlugin

    return EquisLoaderPlugin(iface)
