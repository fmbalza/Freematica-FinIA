# -*- coding: utf-8 -*-
from odoo import models, fields


class FiniaBankMovement(models.Model):
    """Extiende finia.bank.movement (finIA_backend) con la posibilidad de
    repartir un mismo movimiento entre varias cuentas contables reales de
    Freematica, cada una con su propio importe (ver
    freematica_bank_movement_account_line.py) — igual que un movimiento ya
    puede repartirse entre varias facturas vía finia.bank.movement.match."""
    _inherit = 'finia.bank.movement'

    freematica_account_line_ids = fields.One2many(
        'freematica.bank.movement.account.line', 'movement_id',
        string='Cuentas contables asignadas',
    )
