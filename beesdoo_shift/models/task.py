# -*- coding: utf-8 -*-
import json
from datetime import datetime, timedelta

from openerp import _, api, fields, models
from openerp.exceptions import UserError, ValidationError


class TaskStage(models.Model):
    _name = 'beesdoo.shift.stage'
    _order = 'sequence asc'

    name = fields.Char()
    sequence = fields.Integer()
    color = fields.Integer()
    code = fields.Char(readonly=True)

    @api.multi
    def unlink(self):
        raise UserError(_("You Cannot delete Task Stage"))


class Task(models.Model):
    _name = 'beesdoo.shift.shift'

    _inherit = ['mail.thread']

    _order = "start_time asc"

    name = fields.Char(track_visibility='always')
    task_template_id = fields.Many2one('beesdoo.shift.template')
    planning_id = fields.Many2one(related='task_template_id.planning_id', store=True)
    task_type_id = fields.Many2one('beesdoo.shift.type', string="Task Type")
    worker_id = fields.Many2one('res.partner', track_visibility='onchange',
                                domain=[
                                    ('eater', '=', 'worker_eater'),
                                    ('working_mode', 'in', ('regular', 'irregular')),
                                    ('state', 'not in', ('unsubscribed', 'resigning')),
                                ])
    start_time = fields.Datetime(track_visibility='always', index=True)
    end_time = fields.Datetime(track_visibility='always')
    stage_id = fields.Many2one('beesdoo.shift.stage', required=True, track_visibility='onchange', default=lambda self: self.env.ref('beesdoo_shift.open'))
    super_coop_id = fields.Many2one('res.users', string="Super Cooperative", domain=[('partner_id.super', '=', True)], track_visibility='onchange')
    color = fields.Integer(related="stage_id.color", readonly=True)
    # TODO: Maybe is_regular and is_compensation must be merged in a
    # selection field as they are mutually exclusive.
    is_regular = fields.Boolean(default=False, string="Regular shift")
    is_compensation = fields.Boolean(default=False, string="Compensation shift")
    replaced_id = fields.Many2one('res.partner', track_visibility='onchange', domain=[('eater', '=', 'worker_eater')])
    revert_info = fields.Text(copy=False)
    working_mode = fields.Selection(related='worker_id.working_mode')

    def _compensation_validation(self, task):
        """
        Raise a validation error if the fields is_regular and
        is_compensation are not properly set.
        """
        if (task.is_regular == task.is_compensation
                or not (task.is_regular or task.is_compensation)):
            raise ValidationError(
                "You must choose between Regular Shift or "
                "Compensation Shift."
            )

    @api.constrains('is_regular', 'is_compensation')
    def _check_compensation(self):
        for task in self:
            if task.working_mode == 'regular':
                self._compensation_validation(task)

    @api.constrains('worker_id')
    def _check_worker_id(self):
        """
        When worker_id changes we need to check whether is_regular
        and is_compensation are set correctly.
        When worker_id is set to a worker that doesn't need field
        is_regular and is_compensation, these two fields are set to
        False.
        """
        for task in self:
            if task.working_mode == 'regular':
                self._compensation_validation(task)
            else:
                task.write({
                    'is_regular': False,
                    'is_compensation': False,
                })

    def message_auto_subscribe(self, updated_fields, values=None):
        self._add_follower(values)
        return super(Task, self).message_auto_subscribe(updated_fields, values=values)

    def _add_follower(self, vals):
        if vals.get('worker_id'):
            worker = self.env['res.partner'].browse(vals['worker_id'])
            self.message_subscribe(partner_ids=worker.ids)

    @api.model
    def _read_group_stage_id(self, ids, domain, read_group_order=None, access_rights_uid=None):
        res  = self.env['beesdoo.shift.stage'].search([]).name_get()
        fold = dict.fromkeys([r[0] for r in res], False)
        return res, fold

    _group_by_full = {
        'stage_id': _read_group_stage_id,
    }

    #TODO button to replaced someone
    @api.model
    def unsubscribe_from_today(self, worker_ids, today=None, end_date=None):
        today = today or fields.Date.today()
        today += ' 00:00:00'
        if end_date:
            end_date += ' 23:59:59'
        # date_domain = [('worker_id', 'in', worker_ids), ('start_time', '>=', today)]
        date_domain = [('start_time', '>=', today)]
        if end_date:
            date_domain.append(('end_time', '<=', end_date))
        to_unsubscribe = self.search([('worker_id', 'in', worker_ids)] + date_domain)

        to_unsubscribe.write({'worker_id': False})
        # What about replacement ?
        # Remove worker, replaced_id and regular
        to_unsubscribe_replace = self.search([('replaced_id', 'in', worker_ids)] + date_domain)
        to_unsubscribe_replace.write({'worker_id': False, 'replaced_id': False})

        # If worker is Super cooperator, remove it from planning
        super_coop_ids = self.env['res.users'].search(
            [('partner_id', 'in', worker_ids), ('super', '=', True)]).ids

        if super_coop_ids:
            to_unsubscribe_super_coop = self.search(
                [('super_coop_id', 'in', super_coop_ids)] + date_domain)
            to_unsubscribe_super_coop.write({'super_coop_id': False})

    @api.multi
    def write(self, vals):
        """
            Overwrite write to track stage change
            If worker is changer:
               Revert for the current worker
               Change the worker info
               Compute stage change for the new worker
        """
        if 'worker_id' in vals:
            for rec in self:
                if rec.worker_id.id != vals['worker_id']:
                    rec._revert()
                    # To satisfy the constrains on worker_id, it must be
                    # accompanied by the change in is_regular and
                    # is_compensation field.
                    super(Task, rec).write({
                        'worker_id': vals['worker_id'],
                        'is_regular': vals.get('is_regular', rec.is_regular),
                        'is_compensation': vals.get('is_compensation',
                                                    rec.is_compensation),
                    })
                    rec._update_stage(rec.stage_id.id)
        if 'stage_id' in vals:
            for rec in self:
                if vals['stage_id'] != rec.stage_id.id:
                    rec._update_stage(vals['stage_id'])
        return super(Task, self).write(vals)

    def _set_revert_info(self, data, status):
        data_new = {
            'status_id': status.id,
            'data' : {k: data.get(k, 0) * -1 for k in ['sr', 'sc', 'irregular_absence_counter']}
        }
        if data.get('irregular_absence_date'):
            data_new['data']['irregular_absence_date'] = False

        self.write({'revert_info': json.dumps(data_new)})

    def _revert(self):
        if not self.revert_info:
            return
        data = json.loads(self.revert_info)
        self.env['cooperative.status'].browse(data['status_id']).sudo()._change_counter(data['data'])
        self.revert_info = False

    def _update_stage(self, new_stage):
        self.ensure_one()
        self._revert()
        update = int(self.env['ir.config_parameter'].get_param('always_update', False))

        new_stage = self.env['beesdoo.shift.stage'].browse(new_stage)
        data = {}
        DONE = self.env.ref('beesdoo_shift.done')
        ABSENT = self.env.ref('beesdoo_shift.absent')
        EXCUSED = self.env.ref('beesdoo_shift.excused')
        NECESSITY = self.env.ref('beesdoo_shift.excused_necessity')

        if not (self.worker_id or self.replaced_id) and new_stage in (DONE, ABSENT, EXCUSED, NECESSITY):
            raise UserError(_("You cannot change to the status %s if the is no worker defined on the shift") % new_stage.name)

        if update or not (self.worker_id or self.replaced_id):
            return

        if self.worker_id.working_mode == 'regular':
            if not self.replaced_id: #No replacement case
                status = self.worker_id.cooperative_status_ids[0]
            else:
                status = self.replaced_id.cooperative_status_ids[0]

            if new_stage == DONE and not self.is_regular:
                if status.sr < 0:
                    data['sr'] = 1
                elif status.sc < 0:
                    data['sc'] = 1
                else:
                    data['sr'] = 1

            if new_stage == ABSENT and not self.replaced_id:
                data['sr'] = - 1
                if status.sr <= 0:
                    data['sc'] = -1
            if new_stage == ABSENT and self.replaced_id:
                data['sr'] = -1

            if new_stage == EXCUSED:
                data['sr'] = -1

        elif self.worker_id.working_mode == 'irregular':
            status = self.worker_id.cooperative_status_ids[0]
            if new_stage == DONE or new_stage == NECESSITY:
                data['sr'] = 1
                data['irregular_absence_date'] = False
                data['irregular_absence_counter'] = 1 if status.irregular_absence_counter < 0 else 0
            if new_stage == ABSENT or new_stage == EXCUSED:
                data['sr'] = -1
                data['irregular_absence_date'] = self.start_time[:10]
                data['irregular_absence_counter'] = -1

        else:
            raise UserError(_("The worker has not a proper working mode define, please check the worker is subscribed"))
        status.sudo()._change_counter(data)
        self._set_revert_info(data, status)

    @api.model
    def _cron_send_weekly_emails(self):
        """
        Send a summary email for all workers
        if they have a shift planned during the week.
        """
        tasks = self.env["beesdoo.shift.shift"]
        shift_summary_mail_template = self.env.ref(
            "beesdoo_shift.email_template_shift_summary", False
        )

        start_time = datetime.now() + timedelta(days=1)
        end_time = datetime.now() + timedelta(days=7)

        confirmed_tasks = tasks.search(
            [
                ("start_time", ">", start_time.strftime("%Y-%m-%d 00:00:01")),
                ("start_time", "<", end_time.strftime("%Y-%m-%d 23:59:59")),
                ("worker_id", "!=", False),
                ("stage_id", "=", self.env.ref("beesdoo_shift.open").id),
            ]
        )

        for rec in confirmed_tasks:
            shift_summary_mail_template.send_mail(rec.id, True)
