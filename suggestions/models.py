from datetime import datetime
import json

from django.contrib.auth.models import User
from django.contrib.contenttypes.models import ContentType
from django.contrib.contenttypes import generic
from django.db import models
from django.utils.translation import ugettext_lazy as _

from .managers import SuggestionsManager
from .utils import load_model_or_instance


class Suggestion(models.Model):
    """Data improvement suggestions.  Designed to implement suggestions queue
    for content editors.

    A suggestion can be either:

    * Automatically applied once approved (for that data needs to to supplied
      and action be one of: ADD, REMOVE, SET. If the the field to be
      modified is a relation manger, `subject` should be provided as
      well.
    * Manually applied, in that case a content should be provided for
      `content`.

    The model is generic is possible, and designed for building custom
    suggestion forms for each content type.


    Some scenarious:

    * User allowed to enter a genric text comment (those can't be auto applied)
        - action: FREE_TEXT
        - content: Requires the comment (not empty)

    * Suggest a model instance for m2m relation (e.g: add Member to Committee):
        - action: ADD
        - subject: the object to work upon (e.g: Committee instance)
        - field: m2m relation name on subject (e.g: 'members')
        - suggested_object: Instance to add to the relation (Member instance)

    * Suggest instance for ForeignKey (e.g: suggest Member's current_party):
        - action: SET
        - subject: the object to work upon (e.g: Member instance)
        - field: FK field name in subject (e.g: 'current_party')
        - suggested_object: Party instance for the ForeignKey

    * Set Model's text field value (e.g: Member's website):
        - action: SET
        - subject: the object to work upon (e.g: Member instance)
        - field: Field name in subject (e.g: 'website')
        - content: The content for the field
    """

    NEW, FIXED, WONTFIX = 0, 1, 2

    RESOLVE_CHOICES = (
        (NEW, _('New')),
        (FIXED, _('Fixed')),
        (WONTFIX, _("Won't Fix")),
    )

    suggested_at = models.DateTimeField(
        _('Suggested at'), blank=True, default=datetime.now, db_index=True)
    suggested_by = models.ForeignKey(User, related_name='suggestions')

    comment = models.TextField(blank=True, null=True)

    resolved_at = models.DateTimeField(_('Resolved at'), blank=True, null=True)
    resolved_by = models.ForeignKey(
        User, related_name='resolved_suggestions', blank=True, null=True)
    resolved_status = models.IntegerField(
        _('Resolved status'), db_index=True, default=NEW,
        choices=RESOLVE_CHOICES)

    objects = SuggestionsManager()

    class Meta:
        verbose_name = _('Suggestion')
        verbose_name_plural = _('Suggestions')

    @property
    def subject(self):
        """
        Return the subject instance or Model. Allows exceptions to bubble up
        (important for validation in clean() method).

        :rtype: a model instance or Model if found
        :raises: DoesNotExist for invalid pk

        """
        model_or_instance = self.content.get('subject')

        if not model_or_instance:
            return

        return load_model_or_instance(*model_or_instance)

    def auto_apply(self, resolved_by):

        if not self.actions.count():
            raise ValueError("Can't be auto applied, no actions")

        for action in self.actions.all():
            action.auto_apply()

        self.resolved_by = resolved_by
        self.resolved_status = self.FIXED
        self.resolved_at = datetime.now()

        self.save()


class SuggestedAction(models.Model):
    """Suggestion can be of multiple action"""

    ADD, REMOVE, SET, CREATE = range(4)

    SUGGEST_CHOICES = (
        (ADD, _('Add related object to m2m relation or new model instance')),
        (REMOVE, _('Remove related object from m2m relation')),
        (SET, _('Set field value. For m2m _replaces_ (use ADD if needed)')),
        (CREATE, _('Create new model instance')),
    )

    suggestion = models.ForeignKey(Suggestion, related_name='actions')
    action = models.PositiveIntegerField(
        _('Suggestion type'), choices=SUGGEST_CHOICES)

    # The Model instance (or model itself in case of create) to work on
    subject_type = models.ForeignKey(ContentType, related_name='action_subjects')
    subject_id = models.PositiveIntegerField(
        blank=True, null=True, help_text=_('Can be blank, for create operations'))
    subject = generic.GenericForeignKey(
        'subject_type', 'subject_id')

    def auto_apply(self, subject=None):
        """Auto apply the action. subject is optional, and needs to passed in
        case if adding to m2m after create.

        """
        work_on = subject or self.subject

        actions = {
            self.SET: self.do_set,
            self.ADD: self.do_add,
            self.REMOVE: self.do_remove,
        }

        doer = actions.get(self.action)
        return doer(work_on)

    @property
    def action_params(self):
        return (x.field_and_value for x in self.action_fields.all())

    def do_set(self, subject):
        "Set subject fields"
        for field, value in self.action_params:
            setattr(subject, field, value)
        subject.save()
        return subject

    def do_add(self, subject):
        "Add an instance to subject's m2m attribute"

        for fname, value in self.action_params:
            field, model, direct, m2m = subject._meta.get_field_by_name(fname)

            if not m2m:
                raise ValueError("{0} can be auto applied only on m2m".format(
                    self.get_action_display()
                ))
            getattr(subject, fname).add(value)

        return subject

    def do_remove(self, subject):
        "Remove an instance to subject's m2m attribute"

        for fname, value in self.action_params:
            field, model, direct, m2m = subject._meta.get_field_by_name(fname)

            if not m2m:
                raise ValueError("{0} can be auto applied only on m2m".format(
                    self.get_action_display()
                ))
            getattr(subject, fname).remove(value)


class ActionFields(models.Model):
    """Fields for each suggestion"""

    action = models.ForeignKey(SuggestedAction, related_name='action_fields')
    name = models.CharField(
        _('Field or relation set name'), null=False, blank=False, max_length=50)

    # general value
    value = models.TextField(blank=True, null=True)

    # In case value is a related object
    value_type = models.ForeignKey(
        ContentType, related_name='action_values', blank=True, null=True)
    value_id = models.PositiveIntegerField(blank=True, null=True)
    value_object = generic.GenericForeignKey('value_type', 'value_id')

    @property
    def field_and_value(self):
        "Return a tuple of field name and actual value"

        if self.value_id is not None:
            return self.name, self.value_object

        return self.name, json.loads(self.value)
