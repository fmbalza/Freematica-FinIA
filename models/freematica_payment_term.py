# -*- coding: utf-8 -*-
import calendar
from datetime import date

from dateutil.relativedelta import relativedelta

from odoo import fields, models


def freematica_fecha_vencimiento(fecha_factura, dias, regla):
    """Vencimiento de una factura según los días de su forma de pago.

    - 'meses': cada 30 días cuentan como un mes natural (Servinet, 2026-10:
      factura del 31/07 a 30 días vence el 31/08). Si los días no son
      múltiplo de 30 (10, 50, 75), se suman como días naturales.
    - 'fin_mes': como 'meses' y se lleva al último día de ese mes.
    - 'dias': días naturales (31/07 + 30 = 30/08).
    """
    if not dias:
        return fecha_factura
    if regla == 'dias' or dias % 30:
        vencimiento = fecha_factura + relativedelta(days=dias)
    else:
        vencimiento = fecha_factura + relativedelta(months=dias // 30)
    if regla == 'fin_mes':
        vencimiento = date(vencimiento.year, vencimiento.month,
                           calendar.monthrange(vencimiento.year, vencimiento.month)[1])
    return vencimiento


class FreematicaPaymentTerm(models.Model):
    """Formas de pago de proveedores de Freematica (COD_FPAGO de la
    localización del proveedor), con los días para calcular el vencimiento
    de la Cartera. El API no expone este catálogo: se cargó con la tabla
    que facilitó Servinet (2026-10-27) y se mantiene desde Odoo."""
    _name = 'freematica.payment.term'
    _description = 'Forma de pago Freematica'
    _order = 'code'
    _sql_constraints = [
        ('code_unique', 'unique(code)', 'Ya existe una forma de pago con ese código.'),
    ]

    code = fields.Char(string='Código', required=True)
    name = fields.Char(string='Descripción', required=True)
    days = fields.Integer(string='Días', help='Días hasta el vencimiento, desde la fecha de la factura.')
    active = fields.Boolean(default=True)
