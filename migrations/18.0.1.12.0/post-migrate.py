# -*- coding: utf-8 -*-
"""Emparejamientos por nombre con NIF contradictorio (06/11/2026). Antes, si
el NIF del proveedor no estaba en Freematica, se buscaba por parecido de
nombre y además se copiaba el NIF de la ficha encontrada en el proveedor
(Segre Motors -> Soteras Motor). Se deshacen esos casos: el proveedor
recupera su NIF real del OCR y se vuelve a emparejar con las reglas nuevas
(queda "Sin coincidencia" si su NIF no está en Freematica)."""
import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    Vendor = env['finia.ocr.vendor']
    own = Vendor._freematica_own_nifs()
    normalize = lambda v: ''.join(ch for ch in (v or '').upper() if ch.isalnum())  # noqa: E731
    for vendor in Vendor.search([('freematica_match_state', '=', 'coincidencia_automatica')]):
        provider = vendor.freematica_provider_id
        ocr_nif = normalize(vendor.vat)
        if not provider or not ocr_nif or ocr_nif in own or ocr_nif == provider.normalized_nif:
            continue
        before = (vendor.freematica_cod_aux, vendor.nif)
        vals = {'freematica_match_state': 'no_intentado', 'freematica_provider_id': False}
        if normalize(vendor.nif) == provider.normalized_nif:
            vals['nif'] = False
        if vendor.freematica_cod_aux == provider.cod_pro:
            vals['freematica_cod_aux'] = False
        vendor.write(vals)
        vendor._freematica_resolve_provider()
        _logger.warning('Freematica: proveedor %s (%s) estaba emparejado por nombre con %s %s pese a tener '
                        'NIF %s; ahora %s %s', vendor.id, vendor.name, before, provider.nombre_pro, vendor.vat,
                        vendor.freematica_match_state, vendor.freematica_cod_aux)
