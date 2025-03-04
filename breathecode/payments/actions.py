import ast
from datetime import datetime
from functools import cache
import re
from typing import Optional, Type
from dateutil.relativedelta import relativedelta
from django.utils import timezone
from django.db.models.query_utils import Q
from django.db.models import Sum, QuerySet
from django.core.handlers.wsgi import WSGIRequest
from pytz import UTC
from django.contrib.auth.models import User

from breathecode.admissions.models import Academy, Cohort, CohortUser, Syllabus
from breathecode.authenticate.actions import get_user_settings
from breathecode.authenticate.models import UserSetting
from breathecode.utils.attr_dict import AttrDict
from breathecode.utils.i18n import translation
from breathecode.utils.validation_exception import ValidationException
from rest_framework.request import Request

from .models import (SERVICE_UNITS, AcademyService, Bag, Consumable, Currency, EventTypeSet, FinancingOption,
                     MentorshipServiceSet, Plan, PlanFinancing, Service, ServiceItem, Subscription)
from breathecode.utils import getLogger

logger = getLogger(__name__)


def calculate_relative_delta(unit: float, unit_type: str):
    delta_args = {}
    if unit_type == 'DAY':
        delta_args['days'] = unit

    elif unit_type == 'WEEK':
        delta_args['weeks'] = unit

    elif unit_type == 'MONTH':
        delta_args['months'] = unit

    elif unit_type == 'YEAR':
        delta_args['years'] = unit

    return relativedelta(**delta_args)


class PlanFinder:
    cohort: Optional[Cohort] = None
    syllabus: Optional[Syllabus] = None

    def __init__(self, request: Request, lang: Optional[str] = None, query: Optional[Q] = None) -> None:
        self.request = request
        self.query = query

        if lang:
            self.lang = lang

        else:
            self.lang = request.META.get('HTTP_ACCEPT_LANGUAGE')

        if not self.lang and request.user.id:
            settings = get_user_settings(request.user.id)
            self.lang = settings.lang

        if not self.lang:
            self.lang = 'en'

        self.academy_slug = request.GET.get('academy') or request.data.get('academy')

        if cohort := request.GET.get('cohort') or request.data.get('cohort'):
            self.cohort = self._get_instance(Cohort, cohort, self.academy_slug)

        if syllabus := request.GET.get('syllabus') or request.data.get('syllabus'):
            self.syllabus = self._get_instance(Syllabus, syllabus, self.academy_slug)

    def _get_pk(self, pk):
        if isinstance(pk, int) or pk.isnumeric():
            return int(pk)

        return 0

    def _get_instance(self,
                      model: Type[Cohort | Syllabus],
                      pk: str,
                      academy: Optional[str] = None) -> Optional[Cohort | Syllabus]:
        args = []
        kwargs = {}

        if isinstance(pk, int) or pk.isnumeric():
            kwargs['id'] = int(pk)
        else:
            kwargs['slug'] = pk

        if academy and model == Syllabus:
            args.append(
                Q(academy_owner__slug=academy) | Q(academy_owner__id=self._get_pk(academy))
                | Q(private=False))

        elif academy and model == Cohort:
            args.append(Q(academy__slug=academy) | Q(academy__id=self._get_pk(academy)))

        resource = model.objects.filter(*args, **kwargs).first()
        if not resource:
            raise ValidationException(
                translation(self.lang,
                            en=f'{model.__name__} not found',
                            es=f'{model.__name__} no encontrada',
                            slug=f'{model.__name__.lower()}-not-found'))

        return resource

    def _cohort_handler(self, on_boarding: Optional[bool] = None, auto: bool = False):
        additional_args = {}

        if on_boarding is not None:
            additional_args['is_onboarding'] = on_boarding

        if not self.cohort.syllabus_version:
            return Plan.objects.none()

        if not additional_args and auto:
            additional_args['is_onboarding'] = not CohortUser.objects.filter(
                cohort__syllabus_version__syllabus=self.cohort.syllabus_version.syllabus).exists()

        args = (self.query, ) if self.query else tuple()
        plans = Plan.objects.filter(*args,
                                    available_cohorts__id=self.cohort.id,
                                    available_cohorts__stage__in=['INACTIVE', 'PREWORK'],
                                    **additional_args).distinct()

        return plans

    def _syllabus_handler(self, on_boarding: Optional[bool] = None, auto: bool = False):
        additional_args = {}

        if on_boarding is not None:
            additional_args['is_onboarding'] = on_boarding

        if not additional_args and auto:
            additional_args['is_onboarding'] = not CohortUser.objects.filter(
                cohort__syllabus_version__syllabus=self.syllabus).exists()

        args = (self.query, ) if self.query else tuple()
        plans = Plan.objects.filter(*args,
                                    available_cohorts__syllabus_version__syllabus=self.syllabus,
                                    available_cohorts__stage__in=['INACTIVE', 'PREWORK'],
                                    **additional_args).distinct()

        return plans

    def get_plans_belongs(self, on_boarding: Optional[bool] = None, auto: bool = False):
        if self.syllabus:
            return self._syllabus_handler(on_boarding, auto)

        if self.cohort:
            return self._cohort_handler(on_boarding, auto)

        raise NotImplementedError('Resource handler not implemented')

    def get_plans_belongs_from_request(self):
        is_onboarding = self.request.data.get('is_onboarding') or self.request.GET.get('is_onboarding')

        additional_args = {}

        if is_onboarding:
            additional_args['is_onboarding'] = is_onboarding

        if not additional_args:
            additional_args['auto'] = True

        return self.get_plans_belongs(**additional_args)


def ask_to_add_plan_and_charge_it_in_the_bag(plan: Plan, user: User, lang: str):
    """
    Ask to add plan to bag, and return if it must be charged or not.
    """
    utc_now = timezone.now()
    plan_have_free_trial = plan.trial_duration and plan.trial_duration_unit

    if plan.is_renewable:
        price = plan.price_per_month or plan.price_per_quarter or plan.price_per_half or plan.price_per_year

    else:
        price = not plan.is_renewable and plan.financing_options.exists()

    subscriptions = Subscription.objects.filter(user=user, plans=plan)

    # avoid bought a free trial for financing if this was bought before
    if not price and plan_have_free_trial and not plan.is_renewable and subscriptions.filter(
            valid_until__gte=utc_now):
        raise ValidationException(
            translation(lang,
                        en='Free trial plans can\'t be bought again',
                        es='Los planes de prueba no pueden ser comprados de nuevo',
                        slug='free-trial-plan-for-financing'),
            code=400,
        )

    # avoid bought a plan if it doesn't have a price yet after free trial
    if not price and subscriptions:
        raise ValidationException(
            translation(lang,
                        en='Free trial plans can\'t be bought more than once',
                        es='Los planes de prueba no pueden ser comprados más de una vez',
                        slug='free-trial-already-bought'),
            code=400,
        )

    # avoid financing plans if it was financed before
    if not plan.is_renewable and PlanFinancing.objects.filter(user=user, plans=plan):
        raise ValidationException(
            translation(lang,
                        en='You already have or had a financing on this plan',
                        es='Ya tienes o tuviste un financiamiento en este plan',
                        slug='plan-already-financed'),
            code=400,
        )

    # avoid to buy a plan if exists a subscription with same plan with remaining days
    if price and plan.is_renewable and subscriptions.filter(
            Q(Q(status='CANCELLED') | Q(status='DEPRECATED'), valid_until=None, next_payment_at__gte=utc_now)
            | Q(valid_until__gte=utc_now)):
        raise ValidationException(
            translation(lang,
                        en='You already have a subscription to this plan',
                        es='Ya tienes una suscripción a este plan',
                        slug='plan-already-bought'),
            code=400,
        )

    # avoid to charge a plan if it has a free trial and was not bought before
    if not price or (plan_have_free_trial and not subscriptions.exists()):
        return False

    # charge a plan if it has a price
    return bool(price)


class BagHandler:

    def __init__(self, request: Request, bag: Bag, lang: str) -> None:
        self.request = request
        self.lang = lang
        self.bag = bag

        self.service_items = request.data.get('service_items')
        self.plans = request.data.get('plans')
        self.selected_cohort = request.data.get('cohort')
        self.selected_event_type_set = request.data.get('event_type_set')
        self.selected_mentorship_service_set = request.data.get('mentorship_service_set')

        # change the selection
        if self.selected_cohort:
            bag.selected_cohorts.clear()

        # change the selection
        if self.selected_event_type_set:
            bag.selected_event_type_sets.clear()

        # change the selection
        if self.selected_mentorship_service_set:
            bag.selected_mentorship_service_sets.clear()

        self.plans_not_found = set()
        self.service_items_not_found = set()
        self.cohorts_not_found = set()

    def _lookups(self, value, offset=''):
        args = ()
        kwargs = {}
        slug_key = f'{offset}slug__in'
        pk_key = f'{offset}id__in'

        values = value.split(',') if isinstance(value, str) and ',' in value else [value]
        for v in values:
            if slug_key not in kwargs and (not isinstance(v, str) or not v.isnumeric()):
                kwargs[slug_key] = []

            if pk_key not in kwargs and (isinstance(v, int) or v.isnumeric()):
                kwargs[pk_key] = []

            if isinstance(v, int) or v.isnumeric():
                kwargs[pk_key].append(int(v))

            else:
                kwargs[slug_key].append(v)

        if len(kwargs) > 1:
            args = (Q(**{slug_key: kwargs[slug_key]}) | Q(**{pk_key: kwargs[pk_key]}), )
            kwargs = {}

        return args, kwargs

    def _more_than_one_generator(self, en, es):
        return translation(self.lang,
                           en=f'You can only select one {en}',
                           es=f'Solo puedes seleccionar una {es}',
                           slug=f'more-than-one-{en}-selected')

    def _validate_selected_resources(self):
        if self.selected_cohort and not isinstance(self.selected_cohort, int) and not isinstance(
                self.selected_cohort, str):
            raise ValidationException(translation(self.lang,
                                                  en='The cohort needs to be a id or slug',
                                                  es='El cohort debe ser un id o slug'),
                                      slug='cohort-not-id-or-slug')

        if self.selected_event_type_set and not isinstance(
                self.selected_event_type_set, int) and not isinstance(self.selected_event_type_set, str):
            raise ValidationException(translation(self.lang,
                                                  en='The event type set needs to be a id or slug',
                                                  es='El event type set debe ser un id o slug'),
                                      slug='event-type-set-not-id-or-slug')

        if self.selected_mentorship_service_set and not isinstance(
                self.selected_mentorship_service_set, int) and not isinstance(
                    self.selected_mentorship_service_set, str):
            raise ValidationException(translation(self.lang,
                                                  en='The mentorship service set needs to be a id or slug',
                                                  es='El mentorship service set debe ser un id o slug'),
                                      slug='mentorship-service-set-not-id-or-slug')

    def _reset_bag(self):
        if 'checking' in self.request.build_absolute_uri():
            self.bag.service_items.clear()
            self.bag.plans.clear()
            self.bag.token = None
            self.bag.expires_at = None

    def _validate_service_items_format(self):
        if isinstance(self.service_items, list):
            for item in self.service_items:
                if not isinstance(item, dict):
                    raise ValidationException(translation(self.lang,
                                                          en='The service item needs to be a object',
                                                          es='El service item debe ser un objeto'),
                                              slug='service-item-not-object')

                if 'how_many' not in item or 'service' not in item or not isinstance(
                        item['how_many'], int) or not isinstance(item['service'], int):
                    raise ValidationException(translation(
                        self.lang,
                        en='The service item needs to have the keys of the integer type how_many and service',
                        es='El service item debe tener las llaves de tipo entero how_many y service'),
                                              slug='service-item-malformed')

    def _get_service_items_that_not_found(self):
        if isinstance(self.service_items, list):
            for service_item in self.service_items:
                kwargs = {}

                if service_item['service'] and (isinstance(service_item['service'], int)
                                                or service_item['service'].isnumeric()):
                    kwargs['id'] = int(service_item['service'])
                else:
                    kwargs['slug'] = service_item['service']

                if not Service.objects.filter(**kwargs):
                    self.service_items_not_found.add(service_item['service'])

    def _validate_just_select_one_resource_per_type(self):
        args, kwargs = tuple(), {}

        if self.selected_cohort:
            args, kwargs = self._lookups(self.selected_cohort)

        if self.selected_cohort and (x := Cohort.objects.filter(*args, **kwargs).first()):
            self.selected_cohort = x

        if self.selected_event_type_set:
            args, kwargs = self._lookups(self.selected_event_type_set)

        if self.selected_event_type_set and (x := EventTypeSet.objects.filter(*args, **kwargs).first()):
            self.selected_event_type_set = x

        if self.selected_mentorship_service_set:
            args, kwargs = self._lookups(self.selected_mentorship_service_set)

        if self.selected_mentorship_service_set and (x := MentorshipServiceSet.objects.filter(
                *args, **kwargs).first()):
            self.selected_mentorship_service_set = x

    def _get_plans_that_not_found(self):
        if isinstance(self.plans, list):
            for plan in self.plans:
                kwargs = {}
                exclude = {}

                if plan and (isinstance(plan, int) or plan.isnumeric()):
                    kwargs['id'] = int(plan)
                else:
                    kwargs['slug'] = plan

                if self.selected_cohort:
                    kwargs['available_cohorts'] = self.selected_cohort

                else:
                    exclude['available_cohorts__id__gte'] = 1

                if not Plan.objects.filter(**kwargs).exclude(**exclude):
                    self.plans_not_found.add(plan)

    def _report_items_not_found(self):
        if self.service_items_not_found or self.plans_not_found or self.cohorts_not_found:
            raise ValidationException(translation(
                self.lang,
                en=f'Items not found: services={self.service_items_not_found}, plans={self.plans_not_found}, '
                f'cohorts={self.cohorts_not_found}',
                es=f'Elementos no encontrados: servicios={self.service_items_not_found}, '
                f'planes={self.plans_not_found}, cohortes={self.cohorts_not_found}',
                slug='some-items-not-found'),
                                      code=404)

    def _add_service_items_to_bag(self):
        if isinstance(self.service_items, list):
            for service_item in self.service_items:
                args, kwargs = self._lookups(service_item['service'])

                service = Service.objects.filter(*args, **kwargs).first()
                service_item, _ = ServiceItem.objects.get_or_create(service=service,
                                                                    how_many=service_item['how_many'])
                self.bag.service_items.add(service_item)

    def _add_plans_to_bag(self):
        if isinstance(self.plans, list):
            for plan in self.plans:
                kwargs = {}

                args, kwargs = self._lookups(plan)

                p = Plan.objects.filter(*args, **kwargs).first()

                if p and p not in self.bag.plans.filter():
                    self.bag.plans.add(p)

    def _validate_just_one_plan(self):
        how_many_plans = self.bag.plans.count()

        if how_many_plans > 1:

            raise ValidationException(self._more_than_one_generator(en='plan', es='plan'), code=400)

    def _validate_buy_plans_or_service_items(self):
        if self.bag.plans.count() and self.bag.service_items.count():
            raise ValidationException(translation(
                self.lang,
                en="You can't select a plan and a services at the same time",
                es='No puedes seleccionar un plan y servicios al mismo tiempo',
                slug='one-plan-and-many-services'),
                                      code=400)

    def _add_resources_to_bag(self):
        if self.selected_cohort:
            if self.bag.selected_cohorts not in self.bag.selected_cohorts.all():
                self.bag.selected_cohorts.add(self.selected_cohort)

    def _ask_to_add_plan_and_charge_it_in_the_bag(self):
        for plan in self.bag.plans.all():
            ask_to_add_plan_and_charge_it_in_the_bag(plan, self.bag.user, self.lang)

    def execute(self):
        self._reset_bag()

        self._validate_selected_resources()
        self._validate_service_items_format()

        self._get_service_items_that_not_found()
        self._add_resources_to_bag()
        self._validate_just_select_one_resource_per_type()
        self._get_plans_that_not_found()
        self._report_items_not_found()
        self._add_service_items_to_bag()
        self._add_plans_to_bag()
        self._validate_just_one_plan()

        self._validate_buy_plans_or_service_items()

        self._ask_to_add_plan_and_charge_it_in_the_bag()

        self.bag.save()


def add_items_to_bag(request, bag: Bag, lang: str):
    return BagHandler(request, bag, lang).execute()


def check_dependencies_in_bag(bag: Bag, lang: str):
    cohorts = bag.selected_cohorts.all()
    mentorship_service_sets = bag.selected_mentorship_service_sets.all()
    event_type_sets = bag.selected_event_type_sets.all()

    pending_cohorts_for_dependency_resolution = set(cohorts)
    pending_mentorship_service_sets_for_dependency_resolution = set(mentorship_service_sets)
    pending_event_type_sets_for_dependency_resolution = set(event_type_sets)

    for service_item in bag.service_items.all():
        service = service_item.service

        if service.type == 'COHORT':
            for cohort in cohorts:
                service = service_item.service

                if not AcademyService.objects.filter(service=service, academy=cohort.academy).first():
                    raise ValidationException(translation(
                        lang,
                        en=f'The service {service.slug} is not available for the cohort {cohort.slug}',
                        es=f'El servicio {service.slug} no está disponible para el cohorte {cohort.slug}',
                        slug='service-not-available-for-cohort'),
                                              code=400)

                pending_cohorts_for_dependency_resolution.discard(cohort)

        if service.type == 'MENTORSHIP_SERVICE_SET':
            for mentorship_service_set in mentorship_service_sets:
                if not AcademyService.objects.filter(service=service,
                                                     academy=mentorship_service_set.academy).first():
                    raise ValidationException(translation(
                        lang,
                        en=f'The service {service.slug} is not available for the mentorship service set '
                        f'{mentorship_service_set.slug}',
                        es=f'El servicio {service.slug} no está disponible para el conjunto '
                        f'de servicios de mentoría {mentorship_service_set.slug}',
                        slug='service-not-available-for-mentorship-service-set'),
                                              code=400)

                if not mentorship_service_set.mentorship_services.count():
                    raise ValidationException(translation(
                        lang,
                        en=f'The mentorship service set {mentorship_service_set.slug} is not ready to be sold',
                        es=f'El conjunto de servicios de mentoría {mentorship_service_set.slug} no está '
                        f'listo para ser vendido',
                        slug='mentorship-service-set-not-ready-to-be-sold'),
                                              code=400)

                pending_mentorship_service_sets_for_dependency_resolution.discard(mentorship_service_set)

        if service.type == 'EVENT_TYPE_SET':
            for event_type_set in event_type_sets:
                if not AcademyService.objects.filter(service=service_item.service,
                                                     academy=event_type_set.academy).first():
                    raise ValidationException(translation(
                        lang,
                        en=f'The service {service.slug} is not available for the event type set '
                        f'{event_type_set.slug}',
                        es=f'El servicio {service.slug} no está disponible para el conjunto '
                        f'de tipos de eventos {event_type_set.slug}',
                        slug='service-not-available-for-event-type-set'),
                                              code=400)

                if not event_type_set.event_types.count():
                    raise ValidationException(translation(
                        lang,
                        en=f'The event type set {event_type_set.slug} is not ready to be sold',
                        es=
                        f'El conjunto de tipos de eventos {event_type_set.slug} no está listo para ser vendido',
                        slug='event-type-set-not-ready-to-be-sold'),
                                              code=400)

                pending_event_type_sets_for_dependency_resolution.discard(event_type_set)

    for plan in bag.plans.all():
        for cohort in cohorts:
            if cohort not in plan.available_cohorts.all():
                raise ValidationException(translation(
                    lang,
                    en=f'The plan {plan.slug} is not available for the cohort {cohort.slug}',
                    es=f'El plan {plan.slug} no está disponible para el cohorte {cohort.slug}',
                    slug='plan-not-available-for-cohort'),
                                          code=400)

            pending_cohorts_for_dependency_resolution.discard(cohort)

    if pending_cohorts_for_dependency_resolution:
        raise ValidationException(translation(
            lang,
            en=f'The cohorts {", ".join([c.slug for c in pending_cohorts_for_dependency_resolution])} '
            f'are not available for any selected service',
            es=f'Los cohortes {", ".join([c.slug for c in pending_cohorts_for_dependency_resolution])} '
            f'no están disponibles para ningún servicio seleccionado',
            slug='cohorts-not-available-for-any-selected-plan-or-service'),
                                  code=400)

    if pending_mentorship_service_sets_for_dependency_resolution:
        raise ValidationException(translation(
            lang,
            en='The mentorship service sets '
            f'{", ".join([mss.slug for mss in pending_mentorship_service_sets_for_dependency_resolution])} '
            f'are not available for any selected service',
            es='Los conjuntos de servicios de mentoría '
            f'{", ".join([mss.slug for mss in pending_mentorship_service_sets_for_dependency_resolution])} '
            f'no están disponibles para ningún servicio seleccionado',
            slug='mentorship-service-sets-not-available-for-any-selected-plan-or-service'),
                                  code=400)

    if pending_event_type_sets_for_dependency_resolution:
        raise ValidationException(translation(
            lang,
            en='The event type sets '
            f'{", ".join([ets.slug for ets in pending_event_type_sets_for_dependency_resolution])} '
            f'are not available for any selected service',
            es='Los conjuntos de tipos de eventos '
            f'{", ".join([ets.slug for ets in pending_event_type_sets_for_dependency_resolution])} '
            f'no están disponibles para ningún servicio seleccionado',
            slug='event-type-sets-not-available-for-any-selected-plan-or-service'),
                                  code=400)


def get_amount(bag: Bag, currency: Currency, lang: str) -> tuple[float, float, float, float]:
    user = bag.user
    price_per_month = 0
    price_per_quarter = 0
    price_per_half = 0
    price_per_year = 0

    if not currency:
        currency, _ = Currency.objects.get_or_create(code='USD', name='United States dollar')

    for service_item in bag.service_items.all():
        if service_item.service.currency != currency:
            bag.service_items.remove(service_item)
            continue

        price_per_month += service_item.service.price_per_unit * service_item.how_many
        price_per_quarter += service_item.service.price_per_unit * service_item.how_many * 3
        price_per_half += service_item.service.price_per_unit * service_item.how_many * 6
        price_per_year += service_item.service.price_per_unit * service_item.how_many * 12

    for plan in bag.plans.all():
        if plan.currency != currency:
            bag.plans.remove(plan)
            continue

        must_it_be_charged = ask_to_add_plan_and_charge_it_in_the_bag(plan, user, lang)

        # this prices is just used if it are generating a subscription
        if not bag.how_many_installments and (bag.chosen_period != 'NO_SET' or must_it_be_charged):
            price_per_month += (plan.price_per_month or 0)
            price_per_quarter += (plan.price_per_quarter or 0)
            price_per_half += (plan.price_per_half or 0)
            price_per_year += (plan.price_per_year or 0)

    return price_per_month, price_per_quarter, price_per_half, price_per_year


def get_amount_by_chosen_period(bag: Bag, chosen_period: str, lang: str) -> float:
    amount = 0

    if chosen_period == 'MONTH' and bag.amount_per_month:
        amount = bag.amount_per_month

    elif chosen_period == 'QUARTER' and bag.amount_per_quarter:
        amount = bag.amount_per_quarter

    elif chosen_period == 'HALF' and bag.amount_per_half:
        amount = bag.amount_per_half

    elif chosen_period == 'YEAR' and bag.amount_per_year:
        amount = bag.amount_per_year

    # free trial
    if not amount and (bag.amount_per_month or bag.amount_per_quarter or bag.amount_per_half
                       or bag.amount_per_year):
        raise ValidationException(translation(
            lang,
            en=f'The period {chosen_period} is disabled for this bag',
            es=f'El periodo {chosen_period} está deshabilitado para esta bolsa',
            slug='period-disabled-for-bag'),
                                  code=400)

    return amount


def get_bag_from_subscription(subscription: Subscription,
                              settings: Optional[UserSetting] = None,
                              lang: Optional[str] = None) -> Bag:
    bag = Bag()

    if not lang and not settings:
        settings = get_user_settings(subscription.user.id)
        lang = settings.lang
    elif settings:
        lang = settings.lang

    last_invoice = subscription.invoices.filter().last()
    if not last_invoice:
        raise Exception(
            translation(lang,
                        en='Invalid subscription, this has no invoices',
                        es='Suscripción invalida, esta no tiene facturas',
                        slug='subscription-has-no-invoices'))

    bag.status = 'RENEWAL'
    bag.type = 'CHARGE'
    bag.academy = subscription.academy
    bag.currency = last_invoice.currency
    bag.user = subscription.user
    bag.is_recurrent = True
    bag.save()

    for service_item in subscription.service_items.all():
        bag.service_items.add(service_item)

    for plan in subscription.plans.all():
        bag.plans.add(plan)

    bag.amount_per_month, bag.amount_per_quarter, bag.amount_per_half, bag.amount_per_year = get_amount(
        bag, last_invoice.currency, lang)

    bag.save()

    return bag


def get_bag_from_plan_financing(plan_financing: PlanFinancing, settings: Optional[UserSetting] = None) -> Bag:
    bag = Bag()

    if not settings:
        settings = get_user_settings(plan_financing.user.id)

    last_invoice = plan_financing.invoices.filter().last()
    if not last_invoice:
        raise Exception(
            translation(settings.lang,
                        en='Invalid plan financing, this has not charge',
                        es='Plan financing es invalido, este no tiene cargos',
                        slug='plan-financing-has-no-invoices'))

    bag.status = 'RENEWAL'
    bag.type = 'CHARGE'
    bag.academy = plan_financing.academy
    bag.currency = last_invoice.currency
    bag.user = plan_financing.user
    bag.is_recurrent = True
    bag.save()

    for plan in plan_financing.plans.all():
        bag.plans.add(plan)

    return bag


def filter_consumables(request: WSGIRequest,
                       items: QuerySet[Consumable],
                       queryset: QuerySet,
                       key: str,
                       custom_query_key: Optional[str] = None):

    if ids := request.GET.get(key):
        try:
            ids = [int(x) for x in ids.split(',')]
        except:
            raise ValidationException(f'{key} param must be integer')

        query_key = custom_query_key or key
        queryset |= items.filter(**{f'{query_key}__id__in': ids})

    if slugs := request.GET.get(f'{key}_slug'):
        slugs = slugs.split(',')

        query_key = custom_query_key or key
        queryset |= items.filter(**{f'{query_key}__slug__in': slugs})

    if not ids and not slugs:
        query_key = custom_query_key or key
        queryset |= items.filter(**{f'{query_key}__isnull': False})

    queryset = queryset.distinct()
    return queryset


def get_balance_by_resource(queryset: QuerySet, key: str):
    result = []

    ids = {getattr(x, key).id for x in queryset}
    for id in ids:
        current = queryset.filter(**{f'{key}__id': id})
        instance = current.first()
        balance = {}
        items = []
        units = {x[0] for x in SERVICE_UNITS}
        for unit in units:
            per_unit = current.filter(unit_type=unit)
            balance[unit.lower()] = -1 if per_unit.filter(
                how_many=-1).exists() else per_unit.aggregate(Sum('how_many'))['how_many__sum']

        for x in queryset:
            valid_until = x.valid_until
            if valid_until:
                valid_until = re.sub(r'\+00:00$', 'Z', valid_until.replace(tzinfo=UTC).isoformat())

            items.append({
                'id': x.id,
                'how_many': x.how_many,
                'unit_type': x.unit_type,
                'valid_until': x.valid_until,
            })

        result.append({
            'id': getattr(instance, key).id,
            'slug': getattr(instance, key).slug,
            'balance': balance,
            'items': items,
        })
    return result
