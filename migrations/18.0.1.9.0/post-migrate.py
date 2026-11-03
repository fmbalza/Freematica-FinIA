# -*- coding: utf-8 -*-
"""Proveedores emparejados por NIF cuando ese NIF tiene varias fichas en
Freematica: antes se elegía la primera a ciegas. Se vuelven a resolver para
que queden como "varios_nif" y el usuario elija (Servinet, 03/11/2026)."""
import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    Vendor = env['finia.ocr.vendor']
    for vendor in Vendor.search([('freematica_match_state', '=', 'coincidencia_nif')]):
        if len(vendor._freematica_nif_candidates()) > 1:
            previous = vendor.freematica_cod_aux
            vendor._freematica_resolve_provider()
            _logger.info('Freematica: proveedor %s (%s) con NIF en varias fichas; código %s retirado, '
                         'queda para elegir a mano', vendor.id, vendor.name, previous)
