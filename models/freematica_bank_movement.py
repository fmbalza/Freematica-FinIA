# -*- coding: utf-8 -*-
from odoo import models, fields


class FiniaBankMovement(models.Model):
    """Extiende finia.bank.movement (finIA_backend) con un vínculo directo a
    una cuenta contable real de Freematica, para movimientos que no
    corresponden a ninguna factura/albarán/abono/ticket (comisión bancaria,
    nómina, etc.) — alternativa estructurada al texto libre de `category`."""
    _inherit = 'finia.bank.movement'

    freematica_account_id = fields.Many2one(
        'freematica.account', string='Cuenta contable Freematica',
        domain=[('cod_plan', '=', 'PGCS'), ('cta_activa', '=', True), ('subcuenta', '=', True)],
        help='Cuenta contable real asignada directamente a este movimiento cuando no '
             'corresponde a ninguna factura/albarán/abono/ticket. Alternativa '
             'estructurada al texto libre de `category`.',
    )
