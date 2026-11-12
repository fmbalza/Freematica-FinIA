# -*- coding: utf-8 -*-
"""Convierte la copia temporal (pre-migrate) en vínculos proveedor × configuración
para la configuración más antigua (Servinet). Idempotente: no pisa vínculos ya
existentes. Después borra la tabla temporal."""
import logging

_logger = logging.getLogger(__name__)

# columna legacy -> columna del vínculo
_MAP = (
    ('freematica_cod_aux', 'cod_aux'),
    ('freematica_provider_id', 'provider_id'),
    ('freematica_match_score', 'match_score'),
    ('freematica_match_state', 'match_state'),
    ('freematica_prev_provider_id', 'prev_provider_id'),
    ('freematica_prev_match_state', 'prev_match_state'),
    ('freematica_prev_cod_aux', 'prev_cod_aux'),
    ('freematica_prev_default_account', 'prev_default_account'),
    ('freematica_matched_at', 'matched_at'),
    ('freematica_tipo_impuesto', 'tipo_impuesto'),
    ('freematica_has_irpf', 'has_irpf'),
    ('default_accounting_account', 'default_accounting_account'),
)


def migrate(cr, version):
    cr.execute("SELECT to_regclass('_fm_vendor_legacy')")
    if not cr.fetchone()[0]:
        return
    cr.execute("SELECT id FROM freematica_config WHERE active ORDER BY id LIMIT 1")
    row = cr.fetchone()
    if not row:
        _logger.warning('Freematica: sin configuración activa; los datos de proveedores quedan en _fm_vendor_legacy')
        return
    config_id = row[0]
    cr.execute("SELECT column_name FROM information_schema.columns WHERE table_name = '_fm_vendor_legacy'")
    have = {r[0] for r in cr.fetchall()}
    pairs = [(old, new) for old, new in _MAP if old in have]
    cols_new = ', '.join(new for _old, new in pairs)
    cols_old = ', '.join('l.%s' % old for old, _new in pairs)
    # Un proveedor tiene vínculo si algún dato difiere del valor por defecto.
    meaningful = ' OR '.join(
        ("COALESCE(l.%s, 'no_intentado') <> 'no_intentado'" % old) if old == 'freematica_match_state'
        else ("COALESCE(l.%s, 0) <> 0" % old if old == 'freematica_match_score'
              else ("COALESCE(l.%s, false)" % old if old == 'freematica_has_irpf'
                    else "l.%s IS NOT NULL" % old))
        for old, _new in pairs)
    cr.execute("""
        INSERT INTO freematica_vendor_link (vendor_id, config_id, %s, create_date, write_date)
        SELECT l.vendor_id, %%s, %s, now() AT TIME ZONE 'UTC', now() AT TIME ZONE 'UTC'
        FROM _fm_vendor_legacy l
        JOIN finia_ocr_vendor v ON v.id = l.vendor_id
        WHERE (%s)
          AND NOT EXISTS (SELECT 1 FROM freematica_vendor_link x WHERE x.vendor_id = l.vendor_id AND x.config_id = %%s)
    """ % (cols_new, cols_old, meaningful), (config_id, config_id))
    _logger.warning('Freematica: %s vínculos de proveedor creados para la configuración %s', cr.rowcount, config_id)
    cr.execute("DROP TABLE _fm_vendor_legacy")
