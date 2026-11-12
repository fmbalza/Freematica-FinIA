# -*- coding: utf-8 -*-
"""Multiempresa (fase 1): la configuración de Freematica pasa a ser por empresa
de Finia y los catálogos (cuentas, proveedores, artículos) por configuración.

La configuración que ya existe (Servinet, X-Auth-Company 10) se liga a la empresa
"Servinet" de Finia y se le asignan todas las filas actuales de los catálogos,
así que el comportamiento de Servinet no cambia. Es idempotente."""
import logging

_logger = logging.getLogger(__name__)

_CATALOGS = ('freematica_account', 'freematica_provider', 'freematica_article')


def migrate(cr, version):
    cr.execute("SELECT id, empresa_id FROM freematica_config WHERE active ORDER BY id")
    configs = cr.fetchall()
    if not configs:
        return
    config_id, empresa_id = configs[0]
    if not empresa_id:
        cr.execute("SELECT id FROM finia_empresa WHERE name = 'Servinet' AND active ORDER BY id LIMIT 1")
        row = cr.fetchone()
        if row:
            cr.execute("UPDATE freematica_config SET empresa_id = %s WHERE id = %s", (row[0], config_id))
            _logger.warning('Freematica: configuración %s ligada a la empresa Finia %s', config_id, row[0])
        else:
            _logger.warning('Freematica: no hay empresa "Servinet" en Finia; la configuración %s queda sin '
                            'empresa (se usa para los documentos sin empresa).', config_id)
    for table in _CATALOGS:
        cr.execute("UPDATE %s SET config_id = %%s WHERE config_id IS NULL" % table, (config_id,))
        _logger.warning('Freematica: %s filas de %s asignadas a la configuración %s', cr.rowcount, table, config_id)
