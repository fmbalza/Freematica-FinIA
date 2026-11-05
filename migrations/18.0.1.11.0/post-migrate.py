# -*- coding: utf-8 -*-
"""Grupos de proveedores (05/11/2026): Servinet tiene los mismos proveedores
en los grupos 1 y 2 de Freematica con códigos distintos, y Finia los
descargaba juntos (de ahí las "varias fichas con el mismo NIF"). Se
descarga el catálogo con su grupo y se vuelven a emparejar los proveedores
que no se asignaron a mano, ya solo contra el grupo de los asientos."""
import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    config = env['freematica.config'].search([('active', '=', True)], limit=1)
    if not config:
        return
    try:
        config.write({'providers_sync_next_page': 1})
        for _attempt in range(10):
            env['freematica.provider'].sync_from_freematica(config, force=True)
            if (config.providers_sync_next_page or 1) <= 1:
                break
    except Exception as error:  # noqa: BLE001 - sin catálogo se reintenta en el cron
        _logger.error('Freematica: no se pudo descargar el catálogo con grupos: %s', error)
        return
    grupo = config.grupo_aux_proveedor or 1
    Vendor = env['finia.ocr.vendor']
    vendors = Vendor.search([('freematica_match_state', '!=', 'coincidencia_manual')])
    changed = 0
    for vendor in vendors:
        provider = vendor.freematica_provider_id
        if vendor.freematica_match_state == 'varios_nif' or (provider and provider.cod_grupo_pro != grupo):
            before = (vendor.freematica_match_state, vendor.freematica_cod_aux)
            if provider and vendor.freematica_cod_aux == provider.cod_pro:
                vendor.freematica_cod_aux = False
            vendor._freematica_resolve_provider()
            changed += 1
            _logger.info('Freematica: proveedor %s (%s) %s -> %s %s', vendor.id, vendor.name, before,
                         vendor.freematica_match_state, vendor.freematica_cod_aux)
    manual_other = Vendor.search([('freematica_match_state', '=', 'coincidencia_manual'),
                                  ('freematica_provider_id.cod_grupo_pro', 'not in', (grupo, 0))])
    for vendor in manual_other:
        _logger.warning('Freematica: proveedor %s (%s) asignado a mano a una ficha de otro grupo (%s)',
                        vendor.id, vendor.name, vendor.freematica_cod_aux)
    _logger.info('Freematica: %d proveedores reemparejados por grupo %s', changed, grupo)
