# -*- coding: utf-8 -*-
from odoo import fields, models


class FiniaLog(models.Model):
    _inherit = 'finia.log'

    log_type = fields.Selection(selection_add=[
        ('freematica_sent', 'Enviado a Freematica'),
        ('freematica_error', 'Error Freematica'),
    ], ondelete={
        'freematica_sent': 'cascade',
        'freematica_error': 'cascade',
    })
