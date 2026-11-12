# -*- coding: utf-8 -*-
"""Multiempresa (fase 2): los datos de Freematica del proveedor pasan de
columnas de finia_ocr_vendor a freematica.vendor.link (uno por configuración).
Antes de que Odoo cambie los campos, se guarda una copia de lo que hay en una
tabla temporal; el post-migrate la convierte en vínculos y la borra."""
import logging

_logger = logging.getLogger(__name__)

_COLUMNS = (
    'freematica_cod_aux', 'freematica_provider_id', 'freematica_match_score', 'freematica_match_state',
    'freematica_prev_provider_id', 'freematica_prev_match_state', 'freematica_prev_cod_aux',
    'freematica_prev_default_account', 'freematica_matched_at', 'freematica_tipo_impuesto',
    'freematica_has_irpf', 'default_accounting_account',
)


def migrate(cr, version):
    cr.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'finia_ocr_vendor'")
    existing = {row[0] for row in cr.fetchall()}
    columns = [c for c in _COLUMNS if c in existing]
    if not columns:
        return
    cr.execute("DROP TABLE IF EXISTS _fm_vendor_legacy")
    cr.execute("CREATE TABLE _fm_vendor_legacy AS SELECT id AS vendor_id, %s FROM finia_ocr_vendor"
               % ', '.join(columns))
    _logger.warning('Freematica: copia temporal de %s proveedores con las columnas %s', cr.rowcount, columns)
