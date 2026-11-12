# -*- coding: utf-8 -*-
"""Envío de albaranes de compra a Freematica (POST /comp/v2/albaranes-compras).

Reglas de Josep (Freematica, 04/11/2026):
- Un albarán de N líneas son N llamadas que repiten la cabecera; la línea
  se numera en ALCL_NUMLINEA (1, 2, 3...) y ALCL_CODLINEA es C1 si la línea
  tiene artículo y C6 si es solo texto.
- ALCC_NUMDOC lo pone Freematica (el siguiente suyo): se manda 0 en la
  primera línea y el resto usa el número que devuelve. 2026-11-10: el contador
  de Freematica para estos albaranes no tiene numeración ("El contador asociado
  no dispone de numeración", HTTP 400) y Sonia confirma que no hay albaranes
  contabilizados, así que Finia los numera desde 1 con su propia secuencia
  (`freematica.albaran.numdoc`, ajuste "Numerar los albaranes desde Finia").
- CODDOC 2, DELEG 9999, almacén 01, LOC_PAGO 1, SERIEFRA C0, divisa EUR,
  fechas AAAAMMDD, tipos de IVA como en los asientos.
- Si entra responde 200 y si no 400. Para corregir uno ya enviado hay que
  borrarlo en Freematica y volver a enviarlo.

Sonia (Servinet): se envían en cuanto llegan, desde los de octubre de 2026.
Solo los albaranes recibidos en Excel (los únicos que traen precio).

Si un envío falla a mitad, se guarda cuántas líneas entraron y con qué
número, para continuar desde ahí (Reintentar) o empezar de cero tras
borrarlo en Freematica (Reiniciar envío)."""
import logging
import time
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from ..services import freematica_client as client
from .freematica_ocr_vendor import _RETRY_STATES

_logger = logging.getLogger(__name__)

_CPIVA_BY_PERCENT = {0: '0', 4: '3', 10: '2', 21: '1'}
_PERCENT_BY_CPIVA = {code: percent for percent, code in _CPIVA_BY_PERCENT.items()}
_CODLINEA_ARTICULO = 'C1'
_CODLINEA_TEXTO = 'C6'
_FREEMATICA_NUMDOC_SEQUENCE = 'freematica.albaran.numdoc'
_STALE_SENDING = timedelta(minutes=30)
_CRON_BUDGET_SECONDS = 60
_CRON_BATCH = 30


def _fecha(value):
    return value.strftime('%Y%m%d') if value else False


def _clean(values):
    """Quita los campos vacíos (None/False/''), pero no los ceros: NUMDOC 0
    o una cantidad 0 son valores válidos (y en Python 0 == False)."""
    return {k: v for k, v in values.items() if v is not None and v is not False and v != ''}


def _cut(value, size):
    value = (value or '').strip()
    return value[:size] if value else False


class FiniaAlbaran(models.Model):
    _inherit = 'finia.albaran'

    freematica_state = fields.Selection([
        ('no_enviado', 'No enviado'),
        ('bloqueado', 'Falta información'),
        ('enviando', 'Enviando'),
        ('error', 'Error al enviar'),
        ('enviado', 'Enviado'),
    ], string='Estado Freematica', default='no_enviado', copy=False, index=True, tracking=True)
    freematica_numdoc = fields.Integer(string='Nº albarán en Freematica', copy=False, readonly=True)
    freematica_lines_sent = fields.Integer(string='Líneas enviadas', copy=False, readonly=True)
    freematica_lines_total = fields.Integer(string='Líneas a enviar', copy=False, readonly=True)
    freematica_error = fields.Text(string='Error Freematica', copy=False, readonly=True)
    freematica_error_codes = fields.Char(string='Códigos de error', copy=False, readonly=True)
    freematica_action = fields.Text(string='Qué hacer', copy=False, readonly=True)
    freematica_warning = fields.Text(string='Avisos Freematica', copy=False, readonly=True)
    freematica_sent_at = fields.Datetime(string='Enviado a Freematica', copy=False, readonly=True)
    freematica_last_attempt_at = fields.Datetime(string='Último intento', copy=False, readonly=True)

    # ── Comprobaciones ──────────────────────────────────────────────────

    def _freematica_lines(self):
        self.ensure_one()
        return self.line_ids.sorted(lambda l: (l.sequence, l.id))

    def _freematica_line_tax(self, line, articles):
        """(código CPORC_IVA, %) de la línea: el del Excel si lo trae; si no
        (0 %), el del artículo en Freematica; si tampoco, (None, None)."""
        percent = int(round(line.tax_percent or 0))
        if percent:
            return _CPIVA_BY_PERCENT.get(percent), percent
        article = articles.get((line.product_code or '').strip())
        if article and article.cod_iva in _PERCENT_BY_CPIVA:
            return article.cod_iva, _PERCENT_BY_CPIVA[article.cod_iva]
        return None, None

    def _freematica_config(self):
        """Configuración de Freematica de la empresa del albarán (vacía si no hay)."""
        self.ensure_one()
        try:
            return self.env['freematica.config'].sudo().get_config_for(self.empresa_id)
        except UserError:
            return self.env['freematica.config']

    def _freematica_articles(self):
        codes = {(c or '').strip() for c in self.mapped('line_ids.product_code') if c}
        if not codes:
            return {}
        config = self[:1]._freematica_config() if self else self.env['freematica.config']
        if not config:
            return {}
        found = self.env['freematica.article'].sudo()._freematica_scoped(config).search(
            [('cod_articulo', 'in', list(codes))])
        return {a.cod_articulo: a for a in found}

    def _freematica_check_albaran(self, config):
        """Devuelve (problemas, códigos, acciones, avisos). Los problemas
        impiden enviar; los avisos no (p. ej. artículo que no existe en
        Freematica: el alta entra, pero fallará al facturar)."""
        self.ensure_one()
        self = self.with_context(freematica_config_id=config.id) if config else self
        problems, codes, actions, warnings = [], [], [], []

        def add(code, problem, action):
            codes.append(code)
            problems.append(problem)
            if action not in actions:
                actions.append(action)

        vendor = self.ocr_vendor_id
        if not vendor:
            add('missing_vendor', 'El albarán no tiene proveedor.',
                'Asigna el proveedor del albarán en Finia y vuelve a enviarlo.')
        else:
            if not vendor.freematica_cod_aux and vendor.freematica_match_state in _RETRY_STATES:
                vendor.sudo()._freematica_resolve_provider()
            if not vendor.freematica_cod_aux:
                if vendor.freematica_match_state == 'varios_nif':
                    candidates = ', '.join(vendor._freematica_nif_candidates().mapped('cod_pro'))
                    add('vendor_ambiguous',
                        'El NIF de %s tiene varias fichas en Freematica (%s).' % (vendor.name, candidates),
                        'Elige la ficha correcta del proveedor (botón "Asignar" en el aviso de Facturas '
                        'o en la sección "Proveedor Freematica" de una factura suya).')
                else:
                    add('vendor_not_matched',
                        'El proveedor %s no está identificado en Freematica.' % vendor.name,
                        'Da de alta el proveedor en Freematica y pulsa "Actualizar proveedores", o '
                        'asígnalo a mano si existe con otro nombre.')
        if not self.ocr_albaran_date:
            add('missing_date', 'Falta la fecha del albarán.', 'Completa la fecha del albarán en Finia.')
        lines = self._freematica_lines()
        if not lines:
            add('no_lines', 'El albarán no tiene líneas.', 'Revisa el Excel de origen: no se leyó ninguna línea.')

        articles = self._freematica_articles()
        without_code = lines.filtered(lambda l: not (l.product_code or '').strip())
        if without_code and config.albaranes_lineas_sin_articulo == 'bloquear':
            add('lines_without_article',
                '%d de %d líneas no traen código de artículo.' % (len(without_code), len(lines)),
                'El Excel de este proveedor no trae códigos de artículo. Está pendiente de decidir con '
                'Freematica si se envían como línea de texto; hasta entonces no se envía.')
        with_code = lines - without_code
        if config.articles_last_sync_at:
            missing = sorted({(l.product_code or '').strip() for l in with_code} - set(articles))
            if missing:
                warnings.append(
                    '%d artículo(s) no existen en Freematica: %s%s. El albarán entra igual, pero dará '
                    'error al facturarlo: dadlos de alta en Freematica antes de facturar.'
                    % (len(missing), ', '.join(missing[:15]), '…' if len(missing) > 15 else ''))
        no_tax = with_code.filtered(lambda l: self._freematica_line_tax(l, articles)[0] is None)
        if no_tax:
            add('unknown_tax',
                '%d línea(s) con un IVA que no se puede traducir a Freematica (%s).' % (
                    len(no_tax), ', '.join(sorted({'%s %%' % (l.tax_percent or 0) for l in no_tax}))),
                'Revisa el IVA de esas líneas: Freematica solo admite 0, 4, 10 y 21 %.')
        return problems, codes, actions, warnings

    # ── Payload ─────────────────────────────────────────────────────────

    def _freematica_header(self, config, cod_pro, numdoc, fpago):
        self.ensure_one()
        vendor = self.ocr_vendor_id
        fecha = _fecha(self.ocr_albaran_date)
        header = {
            'ALCC_CODEMP': config.x_auth_company,
            'ALCC_DELEG': config.albaranes_delegacion or '9999',
            'ALCC_CODPRO': cod_pro,
            'ALCC_CODDOC': config.albaranes_cod_documento or '2',
            'ALCC_NUMDOC': numdoc or 0,
            'ALCC_FCHDOC': fecha,
            'ALCC_SERIEFRA': config.albaranes_serie_factura or 'C0',
            'ALCC_LOC_PAGO': config.albaranes_loc_pago or '1',
            'ALCC_CODDIVISA': config.albaranes_divisa or 'EUR',
            'ALCC_TIPO_IVA': vendor.freematica_tipo_impuesto or 'S0',
            'ALCC_NUMALB_PRO': _cut(self.ocr_albaran_number, 12),
            'ALCC_FCHALB_PRO': fecha,
            'ALCC_FCHENTREGA': _fecha(self.ocr_delivery_date) or fecha,
            'ALCC_REFERENCIA': _cut(self.excel_invoice_number or self.name, 40),
            'ALCC_OBSERVACIONES': _cut(self.ocr_notes, 255),
        }
        if fpago:
            header['ALCC_FPAGO'] = fpago
        return _clean(header)

    def _freematica_line_payload(self, config, header, line, number, articles):
        code = (line.product_code or '').strip()
        common = {
            'ALCL_CODEMP': header['ALCC_CODEMP'],
            'ALCL_DELEG': header['ALCC_DELEG'],
            'ALCL_CODPRO': header['ALCC_CODPRO'],
            'ALCL_CODDOC': header['ALCC_CODDOC'],
            'ALCL_NUMDOC': header['ALCC_NUMDOC'],
            'ALCL_FCHDOC': header['ALCC_FCHDOC'],
            'ALCL_NUMLINEA': number,
            'ALCL_DESCRIP': _cut(line.name, 40),
        }
        if len((line.name or '').strip()) > 40:
            common['ALCL_DESC_AMPLIADA'] = line.name.strip()
        if not code:
            common['ALCL_CODLINEA'] = _CODLINEA_TEXTO
            return dict(header, **_clean(common))
        cpiva, percent = self._freematica_line_tax(line, articles)
        common.update({
            'ALCL_CODLINEA': _CODLINEA_ARTICULO,
            'ALCL_CODARTIC': code[:14],
            'ALCL_CODALMACEN': config.albaranes_almacen or '01',
            'ALCL_CANTIDAD': line.quantity,
            'ALCL_CAN_RECIB': line.quantity,
            'ALCL_PRECIO': line.unit_price,
            'ALCL_IMPORTE': line.amount,
            'ALCL_TIPO_IVA': header['ALCC_TIPO_IVA'],
            'ALCL_CPORC_IVA': cpiva,
            'ALCL_PPORC_IVA': percent,
            'ALCL_FCHENTREGA': header.get('ALCC_FCHENTREGA'),
        })
        if line.uom:
            common['ALCL_UN_MEDIDA'] = line.uom[:15]
        if line.lot_number:
            common['ALCL_CODLOTE'] = line.lot_number[:40]
        return dict(header, **_clean(common))

    @staticmethod
    def _freematica_extract_numdoc(response):
        data = response.get('data') if isinstance(response, dict) else None
        if isinstance(data, list) and data:
            data = data[0]
        if isinstance(data, dict):
            for key in ('ALCC_NUMDOC', 'ALCL_NUMDOC'):
                try:
                    value = int(float(data.get(key) or 0))
                except (TypeError, ValueError):
                    value = 0
                if value:
                    return value
        return 0

    # ── Envío ───────────────────────────────────────────────────────────

    def _freematica_commit(self):
        if not self.env.context.get('freematica_no_commit'):
            self.env.cr.commit()

    def _freematica_set_failure(self, state, problems, codes, actions, warnings=None):
        self.write({
            'freematica_state': state,
            'freematica_error': '\n'.join(problems),
            'freematica_error_codes': ','.join(codes),
            'freematica_action': '\n'.join(actions),
            'freematica_warning': '\n'.join(warnings or []) or False,
            'freematica_last_attempt_at': fields.Datetime.now(),
        })

    def _freematica_send_one(self, config):
        """Envía (o continúa enviando) este albarán. Devuelve True si quedó
        enviado. Nunca lanza: los fallos quedan en el propio albarán."""
        self.ensure_one()
        self = self.with_context(freematica_config_id=config.id) if config else self
        if self.freematica_state == 'enviado':
            return True
        if self.freematica_state == 'enviando' and self.freematica_last_attempt_at \
                and fields.Datetime.now() - self.freematica_last_attempt_at < _STALE_SENDING:
            return False
        try:
            with self.env.cr.savepoint():
                self.env.cr.execute('SELECT id FROM finia_albaran WHERE id = %s FOR UPDATE NOWAIT', [self.id])
        except Exception:  # noqa: BLE001 - otro proceso lo está enviando
            return False

        problems, codes, actions, warnings = self._freematica_check_albaran(config)
        if problems:
            self._freematica_set_failure('bloqueado', problems, codes, actions, warnings)
            self._freematica_commit()
            return False

        lines = self._freematica_lines()
        articles = self._freematica_articles()
        vendor = self.ocr_vendor_id
        fpago = (vendor.freematica_provider_id._freematica_localizacion_pago(config) or {}).get('fpago') \
            if vendor.freematica_provider_id else False
        token = config._ensure_valid_token()
        client_config = config._as_client_config()
        numdoc = self.freematica_numdoc
        if not numdoc and config.albaranes_numdoc_propio:
            # Se asigna una sola vez y se guarda: un reintento tras un rechazo
            # reutiliza el mismo número en vez de dejar un hueco.
            numdoc = self._freematica_assign_numdoc()
        start = self.freematica_lines_sent if numdoc else 0
        self.write({
            'freematica_state': 'enviando',
            'freematica_lines_total': len(lines),
            'freematica_warning': '\n'.join(warnings) or False,
            'freematica_last_attempt_at': fields.Datetime.now(),
        })
        self._freematica_commit()

        for index in range(start, len(lines)):
            header = self._freematica_header(config, vendor.freematica_cod_aux, numdoc, fpago)
            payload = self._freematica_line_payload(config, header, lines[index], index + 1, articles)
            try:
                response = client.alta_albaran_compra(client_config, payload, token=token)
            except client.FreematicaError as error:
                return self._freematica_fail_line(index, len(lines), numdoc, error.mensaje, warnings)
            except Exception as error:  # noqa: BLE001 - cualquier fallo queda registrado en el albarán
                _logger.exception('Freematica: error enviando el albarán %s', self.id)
                return self._freematica_fail_line(index, len(lines), numdoc, str(error), warnings)
            if not numdoc:
                numdoc = self._freematica_extract_numdoc(response)
                if not numdoc:
                    self.write({'freematica_lines_sent': index + 1})
                    self._freematica_set_failure(
                        'error',
                        ['Freematica aceptó la línea 1 pero no devolvió el número de albarán, así que no se '
                         'pueden añadir las demás líneas a ese mismo albarán.'],
                        ['missing_numdoc'],
                        ['Busca en Freematica el albarán de %s del %s, bórralo y pulsa "Reiniciar envío". '
                         'Avisa a soporte de Finia: hay que revisar la respuesta de Freematica.'
                         % (vendor.name, self.ocr_albaran_date.strftime('%d/%m/%Y'))],
                        warnings)
                    _logger.error('Freematica: alta de albarán %s sin NUMDOC en la respuesta: %s',
                                  self.id, str(response)[:1000])
                    self._freematica_commit()
                    return False
            self.write({'freematica_numdoc': numdoc, 'freematica_lines_sent': index + 1})
            self._freematica_commit()

        self.write({
            'freematica_state': 'enviado',
            'freematica_sent_at': fields.Datetime.now(),
            'freematica_error': False,
            'freematica_error_codes': False,
            'freematica_action': False,
        })
        self._freematica_commit()
        return True

    def _freematica_assign_numdoc(self):
        """Nº de albarán (ALCC_NUMDOC) de la secuencia propia de Finia."""
        self.ensure_one()
        number = self.env['ir.sequence'].sudo().next_by_code(_FREEMATICA_NUMDOC_SEQUENCE)
        if not number:
            raise UserError(_('No existe la secuencia "%s".') % _FREEMATICA_NUMDOC_SEQUENCE)
        self.write({'freematica_numdoc': int(number)})
        return self.freematica_numdoc

    @api.model
    def _freematica_known_error_action(self, message, config):
        """Qué hacer ante los errores de Freematica que ya conocemos."""
        text = (message or '').lower()
        if 'contador' in text and 'numeraci' in text:
            return ('Freematica no tiene numeración configurada para los albaranes de compra (documento %s, '
                    'delegación %s). Hay que pedir a Freematica que la active; después pulsa "Reintentar".'
                    % (config.albaranes_cod_documento, config.albaranes_delegacion))
        if 'articulo' in text or 'artículo' in text:
            return 'Da de alta en Freematica el artículo que indica el error y pulsa "Reintentar".'
        if 'proveedor' in text:
            return 'Revisa en Freematica la ficha del proveedor que indica el error y pulsa "Reintentar".'
        return None

    def _freematica_fail_line(self, index, total, numdoc, message, warnings):
        config = self._freematica_config() if len(self) == 1 else self.env['freematica.config']
        known = self._freematica_known_error_action(message, config)
        if index == 0:
            problem = 'Freematica rechazó el albarán: %s' % message
            action = known or 'Corrige lo que indica el error y pulsa "Reintentar".'
        else:
            problem = 'Freematica rechazó la línea %d de %d: %s' % (index + 1, total, message)
            action = ((known + ' ') if known else '') + ('Las %d primeras líneas ya están en Freematica (albarán nº %s). Corrige el error y '
                      'pulsa "Reintentar" para continuar desde la línea %d, o borra el albarán nº %s en '
                      'Freematica y pulsa "Reiniciar envío" para enviarlo entero de nuevo.'
                      % (index, numdoc, index + 1, numdoc))
        self._freematica_set_failure('error', [problem], ['freematica_rejected'], [action], warnings)
        self._freematica_commit()
        return False

    # ── Acciones ────────────────────────────────────────────────────────

    def action_freematica_send(self):
        """Envío manual (botón): también reintenta los que están en error,
        continuando desde la última línea que entró."""
        results = {}
        for albaran in self:
            config = self.env['freematica.config'].sudo().get_config_for(albaran.empresa_id)
            results[albaran.id] = albaran.sudo()._freematica_send_one(config)
        return results

    def action_freematica_reset(self):
        """Tras borrar el albarán en Freematica: olvida el número y las líneas
        enviadas para mandarlo entero de nuevo. Queda anotado en el registro
        del albarán (número anterior incluido) por si hay que deshacerlo."""
        for albaran in self:
            config = albaran._freematica_config()
            keep_numdoc = bool(config and config.albaranes_numdoc_propio)
            if albaran.freematica_state == 'enviando':
                raise UserError(_('El albarán se está enviando ahora mismo; espera a que termine.'))
            previous = (albaran.freematica_state, albaran.freematica_numdoc, albaran.freematica_lines_sent)
            albaran.write({
                'freematica_state': 'no_enviado',
                # Numerado por Finia: se conserva el nº (el albarán borrado en
                # Freematica libera el suyo) para no dejar hueco en la secuencia.
                'freematica_numdoc': albaran.freematica_numdoc if keep_numdoc else 0,
                'freematica_lines_sent': 0,
                'freematica_sent_at': False,
                'freematica_error': False,
                'freematica_error_codes': False,
                'freematica_action': False,
            })
            albaran.message_post(body=_(
                'Envío a Freematica reiniciado (antes: estado %s, nº %s, %s líneas enviadas).') % previous)
        return True

    @api.model
    def _freematica_pending_albaranes_domain(self, config):
        domain = [('source', '=', 'excel'), ('freematica_state', 'in', ('no_enviado', 'bloqueado'))]
        if config.empresa_id:
            domain.append(('empresa_id', '=', config.empresa_id.id))
        if config.albaranes_fecha_inicio:
            domain.append(('ocr_albaran_date', '>=', config.albaranes_fecha_inicio))
        return domain

    def _freematica_refresh_check(self, config):
        """Comprobación sin enviar nada: marca "Falta información" con el
        motivo, o vuelve a "No enviado" (listo) si ya se corrigió."""
        for albaran in self:
            problems, codes, actions, warnings = albaran._freematica_check_albaran(config)
            if problems:
                albaran._freematica_set_failure('bloqueado', problems, codes, actions, warnings)
            else:
                albaran.write({
                    'freematica_state': 'no_enviado',
                    'freematica_error': False,
                    'freematica_error_codes': False,
                    'freematica_action': False,
                    'freematica_warning': '\n'.join(warnings) or False,
                })

    @api.model
    def _cron_freematica_send_albaranes(self):
        for config in self.env['freematica.config'].sudo().search([('active', '=', True)], order='id'):
            self._freematica_cron_send_for(config)

    @api.model
    def _freematica_cron_send_for(self, config):
        if not config.albaranes_envio_activo:
            # Sin envío automático: solo se comprueba qué falta, para avisar ya.
            pending = self.sudo().search(self._freematica_pending_albaranes_domain(config), limit=500)
            pending._freematica_refresh_check(config)
            return
        started = time.monotonic()
        albaranes = self.sudo().search(self._freematica_pending_albaranes_domain(config),
                                       order='ocr_albaran_date, id', limit=_CRON_BATCH)
        for albaran in albaranes:
            if time.monotonic() - started > _CRON_BUDGET_SECONDS:
                break
            try:
                albaran._freematica_send_one(config)
            except Exception as error:  # noqa: BLE001 - un albarán raro no frena al resto
                self.env.cr.rollback()
                _logger.exception('Freematica: fallo enviando el albarán %s: %s', albaran.id, error)

    def _freematica_api_dict(self):
        self.ensure_one()
        return {
            'id': self.id,
            'state': self.freematica_state,
            'numdoc': self.freematica_numdoc or None,
            'lines_sent': self.freematica_lines_sent,
            'lines_total': self.freematica_lines_total or len(self.line_ids),
            'error': self.freematica_error or None,
            'error_codes': [c for c in (self.freematica_error_codes or '').split(',') if c],
            'action': self.freematica_action or None,
            'warning': self.freematica_warning or None,
            'sent_at': self.freematica_sent_at and fields.Datetime.to_string(self.freematica_sent_at),
            'last_attempt_at': self.freematica_last_attempt_at
            and fields.Datetime.to_string(self.freematica_last_attempt_at),
            'sendable_source': self.source == 'excel',
        }
