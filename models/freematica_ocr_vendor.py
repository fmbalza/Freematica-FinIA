# -*- coding: utf-8 -*-
from odoo import fields, models


class FiniaOcrVendor(models.Model):
    _name = 'finia.ocr.vendor'
    _inherit = 'finia.ocr.vendor'

    nif = fields.Char(
        string='NIF/CIF',
        help='NIF/CIF del proveedor. No lo extrae el OCR hoy; se completa a mano. Usado '
             'como BORRL_NIF al enviar facturas de este proveedor a Freematica.',
    )
    freematica_cod_aux = fields.Char(
        string='Código auxiliar Freematica',
        help='Código auxiliar (BORRL_CODAUX) que identifica a este proveedor dentro de '
             'Freematica. Freematica no expone un endpoint para listar/sincronizar '
             'proveedores, así que este mapeo es manual, uno por proveedor. Obligatorio '
             'para poder enviar sus facturas como asiento contable.',
    )
