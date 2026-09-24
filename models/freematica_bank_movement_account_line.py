# -*- coding: utf-8 -*-
from odoo import models, fields


class FreematicaBankMovementAccountLine(models.Model):
    """Un tramo de un movimiento bancario asignado a una cuenta contable real
    (freematica.account), con su propio importe — permite repartir un mismo
    movimiento entre varias cuentas (p.ej. comisión + IVA de la comisión),
    igual que finia.bank.movement.match permite repartir un movimiento entre
    varias facturas."""
    _name = 'freematica.bank.movement.account.line'
    _description = 'Finia Movimiento Bancario - Cuenta Contable (línea)'
    _order = 'id'

    movement_id = fields.Many2one(
        'finia.bank.movement', string='Movimiento Bancario',
        required=True, ondelete='cascade', index=True,
    )
    freematica_account_id = fields.Many2one(
        'freematica.account', string='Cuenta contable', required=True,
        domain=[('cod_plan', '=', 'PGCS'), ('cta_activa', '=', True), ('subcuenta', '=', True)],
    )
    amount = fields.Float(string='Importe', required=True)
