# -*- coding: utf-8 -*-
from odoo import fields, models

_MATCH_STATES = [
    ('no_intentado', 'No intentado'),
    ('coincidencia_nif', 'Coincidencia por NIF'),
    ('coincidencia_automatica', 'Coincidencia automática (nombre)'),
    ('coincidencia_manual', 'Coincidencia manual'),
    ('varios_nif', 'Varios proveedores con el mismo NIF'),
    ('sin_coincidencia', 'Sin coincidencia'),
]

TAX_TYPES = [
    ('S0', 'S0 · Soportado interior sujeto no exento (habitual)'),
    ('S01', 'S01 · Servicios DTOPE sujeta no exenta (NIF N)'),
    ('S02', 'S02 · Bienes DTOPE sujeta no exenta (NIF N)'),
    ('S1', 'S1 · Sujeto no exento no deducible'),
    ('S10', 'S10 · Sujeto no exento con retención de profesionales'),
    ('S11', 'S11 · Sujeto no exento con retención de alquiler'),
    ('S12', 'S12 · No sujeto, regla de localización (exterior)'),
    ('S15', 'S15 · Sujeto no exento ISP (inversión del sujeto pasivo)'),
    ('S150', 'S150 · Bien de inversión ISP'),
    ('S19', 'S19 · Soportado normal con retención 19%'),
    ('S2', 'S2 · Comunitaria servicios (clave I)'),
    ('S21', 'S21 · Comunitaria bienes (clave A)'),
    ('S25', 'S25 · Autofactura inversión sujeto pasivo'),
    ('S3', 'S3 · Importación de bienes'),
    ('S4', 'S4 · Sujeto exento'),
    ('S5', 'S5 · Autofactura comunitaria bienes'),
    ('S6', 'S6 · Autofactura comunitaria servicios'),
    ('S7', 'S7 · IPSI Ceuta'),
    ('S70', 'S70 · IPSI Ceuta no sujeto'),
    ('S71', 'S71 · IGIC'),
    ('S710', 'S710 · IGIC no sujeto'),
    ('S8', 'S8 · Bien de inversión'),
    ('S9', 'S9 · Retención financiera'),
    ('SP', 'SP · Prorrata'),
    ('SSUP', 'SSUP · Suplidos'),
]


class FreematicaVendorLink(models.Model):
    """Lo que un proveedor de Finia es en UNA empresa de Freematica (una
    configuración): su ficha, el estado del emparejamiento, el tipo de impuesto,
    el IRPF y la cuenta de gasto por defecto. Un proveedor tiene un vínculo por
    configuración; `finia.ocr.vendor` los expone como campos de la configuración
    en curso (contexto `freematica_config_id`)."""
    _name = 'freematica.vendor.link'
    _description = 'Proveedor de Finia en una empresa de Freematica'
    _sql_constraints = [
        ('vendor_config_unique', 'unique(vendor_id, config_id)',
         'El proveedor ya tiene un vínculo con esa configuración de Freematica.'),
    ]

    vendor_id = fields.Many2one('finia.ocr.vendor', required=True, index=True, ondelete='cascade')
    config_id = fields.Many2one('freematica.config', required=True, index=True, ondelete='cascade')
    cod_aux = fields.Char(string='Código auxiliar Freematica')
    provider_id = fields.Many2one('freematica.provider', string='Proveedor Freematica', ondelete='set null')
    match_score = fields.Float(string='Score de coincidencia')
    match_state = fields.Selection(_MATCH_STATES, string='Estado coincidencia', default='no_intentado')
    prev_provider_id = fields.Many2one('freematica.provider', string='Proveedor anterior', ondelete='set null')
    prev_match_state = fields.Selection(_MATCH_STATES, string='Estado coincidencia anterior')
    prev_cod_aux = fields.Char(string='Código auxiliar anterior')
    prev_default_account = fields.Char(string='Cuenta por defecto anterior')
    matched_at = fields.Datetime(string='Última resolución')
    tipo_impuesto = fields.Selection(TAX_TYPES, string='Tipo de impuesto Freematica')
    has_irpf = fields.Boolean(string='Facturas con IRPF')
    default_accounting_account = fields.Char(string='Cuenta contable de gasto por defecto')
