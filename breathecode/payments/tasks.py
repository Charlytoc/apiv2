import logging
from datetime import datetime, timedelta
import os
import traceback
from typing import Optional
from django.utils import timezone

from celery import Task, shared_task
from breathecode.authenticate.actions import get_user_settings

from breathecode.notify import actions as notify_actions
from breathecode.payments import actions
from breathecode.payments.services.stripe import Stripe
from dateutil.relativedelta import relativedelta
from django.db.models import Q
from breathecode.payments.signals import consume_service
from breathecode.utils.decorators import task
from breathecode.utils.i18n import translation

from .models import AbstractIOweYou, Bag, Consumable, ConsumptionSession, Invoice, PlanFinancing, PlanServiceItem, PlanServiceItemHandler, Service, ServiceStockScheduler, Subscription, SubscriptionServiceItem
from breathecode.payments.signals import reimburse_service_units

logger = logging.getLogger(__name__)


class BaseTaskWithRetry(Task):
    autoretry_for = (Exception, )
    #                                           seconds
    retry_kwargs = {'max_retries': 5, 'countdown': 60 * 5}
    retry_backoff = True


def get_app_url():
    return os.getenv('APP_URL', '')


@shared_task(bind=True, base=BaseTaskWithRetry)
def renew_consumables(self, scheduler_id: int):
    """
    The purpose of this function is renew every service items belongs to a subscription.
    """

    def get_resource_lookup(i_owe_you: AbstractIOweYou, service: Service):
        lookups = {}

        key = service.type.lower()
        value = getattr(i_owe_you, f'selected_{key}', None)
        if value:
            lookups[key] = value

        return lookups

    logger.info(f'Starting renew_consumables for service stock scheduler {scheduler_id}')

    if not (scheduler := ServiceStockScheduler.objects.filter(id=scheduler_id).first()):
        logger.error(f'ServiceStockScheduler with id {scheduler_id} not found')
        return

    utc_now = timezone.now()

    # is over
    if (scheduler.plan_handler and scheduler.plan_handler.subscription
            and scheduler.plan_handler.subscription.valid_until
            and scheduler.plan_handler.subscription.valid_until < utc_now):
        logger.error(f'The subscription {scheduler.plan_handler.subscription.id} is over')
        return

    # it needs to be paid
    if (scheduler.plan_handler and scheduler.plan_handler.subscription
            and scheduler.plan_handler.subscription.next_payment_at < utc_now):
        logger.error(
            f'The subscription {scheduler.plan_handler.subscription.id} needs to be paid to renew the '
            'consumables')
        return

    # is over
    if (scheduler.plan_handler and scheduler.plan_handler.plan_financing
            and scheduler.plan_handler.plan_financing.valid_until < utc_now):
        logger.error(f'The plan financing {scheduler.plan_handler.plan_financing.id} is over')
        return

    # it needs to be paid
    if (scheduler.plan_handler and scheduler.plan_handler.plan_financing
            and scheduler.plan_handler.plan_financing.next_payment_at < utc_now):
        logger.error(
            f'The plan financing {scheduler.plan_handler.plan_financing.id} needs to be paid to renew '
            'the consumables')
        return

    if (scheduler.plan_handler and scheduler.plan_handler.plan_financing
            and scheduler.plan_handler.handler.plan.time_of_life
            and scheduler.plan_handler.handler.plan.time_of_life_unit
            and scheduler.plan_handler.plan_financing.created_at + actions.calculate_relative_delta(
                scheduler.plan_handler.handler.plan.time_of_life,
                scheduler.plan_handler.handler.plan.time_of_life_unit) < utc_now):
        logger.info(
            f'The services related to PlanFinancing {scheduler.plan_handler.plan_financing.id} is over')
        return

    # is over
    if (scheduler.subscription_handler and scheduler.subscription_handler.subscription
            and scheduler.subscription_handler.subscription.valid_until < utc_now):
        logger.error(f'The subscription {scheduler.subscription_handler.subscription.id} is over')
        return

    # it needs to be paid
    if (scheduler.subscription_handler and scheduler.subscription_handler.subscription
            and scheduler.subscription_handler.subscription.next_payment_at < utc_now):
        logger.error(
            f'The subscription {scheduler.subscription_handler.subscription.id} needs to be paid to renew '
            'the consumables')
        return

    if (scheduler.valid_until and scheduler.valid_until - timedelta(days=1) < utc_now):
        logger.info(f'The scheduler {scheduler.id} don\'t needs to be renewed')
        return

    service_item = None
    resource_valid_until = None
    selected_lookup = {}

    if scheduler.plan_handler and scheduler.plan_handler.subscription:
        user = scheduler.plan_handler.subscription.user
        service_item = scheduler.plan_handler.handler.service_item
        resource_valid_until = scheduler.plan_handler.subscription.valid_until

        selected_lookup = get_resource_lookup(scheduler.plan_handler.subscription, service_item.service)

    elif scheduler.plan_handler and scheduler.plan_handler.plan_financing:
        user = scheduler.plan_handler.plan_financing.user
        service_item = scheduler.plan_handler.handler.service_item
        resource_valid_until = scheduler.plan_handler.plan_financing.valid_until

        selected_lookup = get_resource_lookup(scheduler.plan_handler.plan_financing, service_item.service)

    elif scheduler.subscription_handler and scheduler.subscription_handler.subscription:
        user = scheduler.subscription_handler.subscription.user
        service_item = scheduler.subscription_handler.service_item
        resource_valid_until = scheduler.subscription_handler.subscription.valid_until

        selected_lookup = get_resource_lookup(scheduler.subscription_handler.subscription,
                                              service_item.service)

    unit = service_item.renew_at
    unit_type = service_item.renew_at_unit

    delta = actions.calculate_relative_delta(unit, unit_type)
    scheduler.valid_until = scheduler.valid_until or utc_now
    scheduler.valid_until = scheduler.valid_until + delta

    if resource_valid_until and scheduler.valid_until and scheduler.valid_until > resource_valid_until:
        scheduler.valid_until = resource_valid_until

    scheduler.save()

    if not selected_lookup:
        logger.error('The Plan not have a resource linked to it '
                     f'for the ServiceStockScheduler {scheduler.id}')
        return

    consumable = Consumable(service_item=service_item,
                            user=user,
                            unit_type=service_item.unit_type,
                            how_many=service_item.how_many,
                            valid_until=scheduler.valid_until,
                            **selected_lookup)

    consumable.save()

    scheduler.consumables.add(consumable)

    key = list(selected_lookup.keys())[0]
    id = selected_lookup[key].id
    name = key.replace('selected_', '').replace('_', ' ')

    logger.info(f'The consumable {consumable.id} for {name} {id} was built')
    logger.info(f'The scheduler {scheduler.id} was renewed')


@shared_task(bind=True, base=BaseTaskWithRetry)
def renew_subscription_consumables(self, subscription_id: int):
    """
    The purpose of this function is renew every service items belongs to a subscription.
    """

    logger.info(f'Starting renew_subscription_consumables for id {subscription_id}')

    if not (subscription := Subscription.objects.filter(id=subscription_id).first()):
        logger.error(f'Subscription with id {subscription_id} not found')
        return

    utc_now = timezone.now()
    if subscription.valid_until and subscription.valid_until < utc_now:
        logger.error(f'The subscription {subscription.id} is over')
        return

    if subscription.next_payment_at < utc_now:
        logger.error(f'The subscription {subscription.id} needs to be paid to renew the consumables')
        return

    for scheduler in ServiceStockScheduler.objects.filter(subscription_handler__subscription=subscription):
        renew_consumables.delay(scheduler.id)

    for scheduler in ServiceStockScheduler.objects.filter(plan_handler__subscription=subscription):
        renew_consumables.delay(scheduler.id)


@shared_task(bind=True, base=BaseTaskWithRetry)
def renew_plan_financing_consumables(self, plan_financing_id: int):
    """
    The purpose of this function is renew every service items belongs to a subscription.
    """

    logger.info(f'Starting renew_plan_financing_consumables for id {plan_financing_id}')

    if not (plan_financing := PlanFinancing.objects.filter(id=plan_financing_id).first()):
        logger.error(f'PlanFinancing with id {plan_financing_id} not found')
        return

    utc_now = timezone.now()
    if plan_financing.valid_until and plan_financing.valid_until < utc_now:
        logger.error(f'The plan financing {plan_financing.id} is over')
        return

    if plan_financing.next_payment_at < utc_now:
        logger.error(f'The PlanFinancing {plan_financing.id} needs to be paid to renew the consumables')
        return

    if plan_financing.plan_expires_at and plan_financing.plan_expires_at < utc_now:
        logger.info(f'The services related to PlanFinancing {plan_financing.id} is over')
        return

    for scheduler in ServiceStockScheduler.objects.filter(plan_handler__plan_financing=plan_financing):
        renew_consumables.delay(scheduler.id)


def fallback_charge_subscription(self, subscription_id: int, exception: Exception):
    if not (subscription := Subscription.objects.filter(id=subscription_id).first()):
        return

    settings = get_user_settings(subscription.user.id)
    utc_now = timezone.now()

    message = f'charge_subscription is failing for the subscription {subscription.id}: '
    message += str(exception)[:250 - len(message)]

    subscription.status = 'ERROR'
    subscription.status_message = message
    subscription.save()

    invoice = subscription.invoices.filter(paid_at__gte=utc_now - timedelta(days=1)).order_by('-id').first()

    if invoice:
        s = Stripe()
        s.set_language_from_settings(settings)
        s.refund_payment(invoice)


@task(bind=True, base=BaseTaskWithRetry, transaction=True, fallback=fallback_charge_subscription)
def charge_subscription(self, subscription_id: int):
    """
    The purpose of this function is just to renew a subscription, not more than this.
    """

    logger.info(f'Starting charge_subscription for subscription {subscription_id}')

    if not (subscription := Subscription.objects.filter(id=subscription_id).first()):
        logger.error(f'Subscription with id {subscription_id} not found')
        return

    utc_now = timezone.now()

    if subscription.valid_until and subscription.valid_until < utc_now:
        logger.error(f'The subscription {subscription.id} is over')
        return

    settings = get_user_settings(subscription.user.id)

    try:
        bag = actions.get_bag_from_subscription(subscription, settings)
    except Exception as e:
        logger.error(f'Error getting bag from subscription {subscription_id}: {e}')
        subscription.status = 'ERROR'
        subscription.status_message = str(e)
        subscription.save()
        return

    amount = actions.get_amount_by_chosen_period(bag, bag.chosen_period, settings.lang)

    try:
        s = Stripe()
        s.set_language_from_settings(settings)
        invoice = s.pay(subscription.user, bag, amount, currency=bag.currency)

    except Exception as e:
        subject = translation(settings.lang,
                              en='Your 4Geeks subscription could not be renewed',
                              es='Tu suscripción 4Geeks no pudo ser renovada')

        message = translation(settings.lang,
                              en='Please update your payment methods',
                              es='Por favor actualiza tus métodos de pago')

        button = translation(settings.lang,
                             en='Please update your payment methods',
                             es='Por favor actualiza tus métodos de pago')

        notify_actions.send_email_message(
            'message', subscription.user.email, {
                'SUBJECT': subject,
                'MESSAGE': message,
                'BUTTON': button,
                'LINK': f'{get_app_url()}/subscription/{subscription.id}',
            })

        bag.delete()

        subscription.status = 'PAYMENT_ISSUE'
        subscription.save()
        return

    subscription.paid_at = utc_now
    subscription.next_payment_at = utc_now + actions.calculate_relative_delta(
        subscription.pay_every, subscription.pay_every_unit)

    subscription.invoices.add(invoice)

    subscription.save()
    value = invoice.currency.format_price(invoice.amount)

    subject = translation(settings.lang,
                          en='Your 4Geeks subscription was successfully renewed',
                          es='Tu suscripción 4Geeks fue renovada exitosamente')

    message = translation(settings.lang, en=f'The amount was {value}', es=f'El monto fue {value}')

    button = translation(settings.lang, en='See the invoice', es='Ver la factura')

    notify_actions.send_email_message(
        'message', invoice.user.email, {
            'SUBJECT': subject,
            'MESSAGE': message,
            'BUTTON': button,
            'LINK': f'{get_app_url()}/subscription/{subscription.id}',
        })

    renew_subscription_consumables.delay(subscription.id)


def fallback_charge_plan_financing(self, plan_financing_id: int, exception: Exception):
    if not (plan_financing := PlanFinancing.objects.filter(id=plan_financing_id).first()):
        return

    settings = get_user_settings(plan_financing.user.id)
    utc_now = timezone.now()

    message = f'charge_plan_financing is failing for the plan financing {plan_financing.id}: '
    message += str(exception)[:250 - len(message)]

    plan_financing.status = 'ERROR'
    plan_financing.status_message = message
    plan_financing.save()

    invoice = plan_financing.invoices.filter(paid_at__gte=utc_now - timedelta(days=1)).order_by('-id').first()

    if invoice:
        s = Stripe()
        s.set_language_from_settings(settings)
        s.refund_payment(invoice)


@task(bind=True, base=BaseTaskWithRetry, transaction=True, fallback=fallback_charge_plan_financing)
def charge_plan_financing(self, plan_financing_id: int):
    """
    The purpose of this function is just to renew a subscription, not more than this.
    """

    logger.info(f'Starting charge_plan_financing for id {plan_financing_id}')

    if not (plan_financing := PlanFinancing.objects.filter(id=plan_financing_id).first()):
        logger.error(f'PlanFinancing with id {plan_financing_id} not found')
        return

    utc_now = timezone.now()

    if plan_financing.valid_until < utc_now:
        logger.error(f'PlanFinancing with id {plan_financing_id} is over')
        return

    settings = get_user_settings(plan_financing.user.id)

    try:
        bag = actions.get_bag_from_plan_financing(plan_financing, settings)
    except Exception as e:
        logger.error(f'Error getting bag from plan financing {plan_financing_id}: {e}')
        plan_financing.status = 'ERROR'
        plan_financing.status_message = str(e)
        plan_financing.save()
        return

    amount = plan_financing.monthly_price

    try:
        s = Stripe()
        s.set_language_from_settings(settings)

        invoice = s.pay(plan_financing.user, bag, amount, currency=bag.currency)

    except Exception as e:
        subject = translation(settings.lang,
                              en='Your 4Geeks subscription could not be renewed',
                              es='Tu suscripción 4Geeks no pudo ser renovada')

        message = translation(settings.lang,
                              en='Please update your payment methods',
                              es='Por favor actualiza tus métodos de pago')

        button = translation(settings.lang,
                             en='Please update your payment methods',
                             es='Por favor actualiza tus métodos de pago')

        notify_actions.send_email_message(
            'message', plan_financing.user.email, {
                'SUBJECT': subject,
                'MESSAGE': message,
                'BUTTON': button,
                'LINK': f'{get_app_url()}/plan-financing/{plan_financing.id}',
            })

        bag.delete()

        plan_financing.status = 'PAYMENT_ISSUE'
        plan_financing.save()
        return

    plan_financing.next_payment_at = utc_now + relativedelta(months=1)
    plan_financing.invoices.add(invoice)
    plan_financing.save()

    value = invoice.currency.format_price(invoice.amount)

    subject = translation(settings.lang,
                          en='Your installment at 4Geeks was successfully charged',
                          es='Tu cuota en 4Geeks fue cobrada exitosamente')

    message = translation(settings.lang, en=f'The amount was {value}', es=f'El monto fue {value}')

    button = translation(settings.lang, en='See the invoice', es='Ver la factura')

    notify_actions.send_email_message(
        'message', invoice.user.email, {
            'SUBJECT': subject,
            'MESSAGE': message,
            'BUTTON': button,
            'LINK': f'{get_app_url()}/plan-financing/{plan_financing.id}',
        })

    renew_plan_financing_consumables.delay(plan_financing.id)


@shared_task(bind=True, base=BaseTaskWithRetry)
def build_service_stock_scheduler_from_subscription(self,
                                                    subscription_id: int,
                                                    user_id: Optional[int] = None):
    """
    This builds the service stock scheduler for a subscription.
    """

    logger.info(
        f'Starting build_service_stock_scheduler_from_subscription for subscription {subscription_id}')

    k = {
        'subscription': 'user__id',
        # service items of
        'handlers': {
            'of_subscription': 'subscription_handler__subscription__user__id',
            'of_plan': 'plan_handler__subscription__user__id',
        },
    }

    additional_args = {
        'subscription': {
            k['subscription']: user_id
        } if user_id else {},
        # service items of
        'handlers': {
            'of_subscription': {
                k['handlers']['of_subscription']: user_id,
            },
            'of_plan': {
                k['handlers']['of_plan']: user_id,
            },
        },
    }

    if not (subscription := Subscription.objects.filter(id=subscription_id, **
                                                        additional_args['subscription']).first()):
        logger.error(f'Subscription with id {subscription_id} not found')
        return

    utc_now = timezone.now()

    for handler in SubscriptionServiceItem.objects.filter(subscription=subscription):
        unit = handler.service_item.renew_at
        unit_type = handler.service_item.renew_at_unit
        delta = actions.calculate_relative_delta(unit, unit_type)
        valid_until = utc_now + delta

        if subscription.next_payment_at and valid_until > subscription.next_payment_at:
            valid_until = subscription.next_payment_at

        if subscription.valid_until and valid_until > subscription.valid_until:
            valid_until = subscription.valid_until

        ServiceStockScheduler.objects.get_or_create(subscription_handler=handler)

    for plan in subscription.plans.all():
        for handler in PlanServiceItem.objects.filter(plan=plan):
            unit = handler.service_item.renew_at
            unit_type = handler.service_item.renew_at_unit
            delta = actions.calculate_relative_delta(unit, unit_type)
            valid_until = utc_now + delta

            if valid_until > subscription.next_payment_at:
                valid_until = subscription.next_payment_at

            if subscription.valid_until and valid_until > subscription.valid_until:
                valid_until = subscription.valid_until

            handler, _ = PlanServiceItemHandler.objects.get_or_create(subscription=subscription,
                                                                      handler=handler)

            ServiceStockScheduler.objects.get_or_create(plan_handler=handler)

    renew_subscription_consumables.delay(subscription.id)


@shared_task(bind=True, base=BaseTaskWithRetry)
def build_service_stock_scheduler_from_plan_financing(self,
                                                      plan_financing_id: int,
                                                      user_id: Optional[int] = None):
    """
    This builds the service stock scheduler for a plan financing.
    """
    logger.info(
        f'Starting build_service_stock_scheduler_from_plan_financing for subscription {plan_financing_id}')

    k = {
        'plan_financing': 'user__id',
        # service items of
        'handlers': {
            'of_subscription': 'subscription_handler__subscription__user__id',
            'of_plan': 'plan_handler__subscription__user__id',
        },
    }

    additional_args = {
        'plan_financing': {
            k['plan_financing']: user_id
        } if user_id else {},
        # service items of
        'handlers': {
            'of_plan': {
                k['handlers']['of_plan']: user_id,
            },
        },
    }

    if not (plan_financing := PlanFinancing.objects.filter(id=plan_financing_id,
                                                           **additional_args['plan_financing']).first()):
        logger.error(f'PlanFinancing with id {plan_financing_id} not found')
        return

    for plan in plan_financing.plans.all():
        for handler in PlanServiceItem.objects.filter(plan=plan):
            unit = handler.service_item.renew_at
            unit_type = handler.service_item.renew_at_unit
            delta = actions.calculate_relative_delta(unit, unit_type)
            valid_until = plan_financing.created_at + delta

            if valid_until > plan_financing.next_payment_at:
                valid_until = plan_financing.next_payment_at

            if plan_financing.plan_expires_at and valid_until > plan_financing.plan_expires_at:
                valid_until = plan_financing.plan_expires_at

            if plan_financing.valid_until and valid_until > plan_financing.valid_until:
                valid_until = plan_financing.valid_until

            handler, _ = PlanServiceItemHandler.objects.get_or_create(plan_financing=plan_financing,
                                                                      handler=handler)

            ServiceStockScheduler.objects.get_or_create(plan_handler=handler)

    renew_plan_financing_consumables.delay(plan_financing.id)


@shared_task(bind=True, base=BaseTaskWithRetry)
def build_subscription(self, bag_id: int, invoice_id: int, start_date: Optional[datetime] = None):
    logger.info(f'Starting build_subscription for bag {bag_id}')

    if not (bag := Bag.objects.filter(id=bag_id, status='PAID', was_delivered=False).first()):
        logger.error(f'Bag with id {bag_id} not found')
        return

    if not (invoice := Invoice.objects.filter(id=invoice_id, status='FULFILLED').first()):
        logger.error(f'Invoice with id {invoice_id} not found')
        return

    months = 1

    if bag.chosen_period == 'QUARTER':
        months = 3

    elif bag.chosen_period == 'HALF':
        months = 6

    elif bag.chosen_period == 'YEAR':
        months = 12

    cohort = bag.selected_cohorts.first()
    plan = bag.plans.first()

    event_type_set = bag.selected_event_type_sets.first()
    if plan and not event_type_set:
        event_type_set = plan.event_type_set

    mentorship_service_set = bag.selected_mentorship_service_sets.first()
    if plan and not mentorship_service_set:
        mentorship_service_set = plan.mentorship_service_set

    subscription_start_at = start_date or invoice.paid_at
    subscription = Subscription.objects.create(user=bag.user,
                                               paid_at=invoice.paid_at,
                                               academy=bag.academy,
                                               selected_cohort=cohort,
                                               selected_event_type_set=event_type_set,
                                               selected_mentorship_service_set=mentorship_service_set,
                                               valid_until=None,
                                               next_payment_at=subscription_start_at +
                                               relativedelta(months=months),
                                               status='ACTIVE')

    subscription.plans.set(bag.plans.all())
    subscription.service_items.set(bag.service_items.all())

    subscription.save()
    subscription.invoices.add(invoice)

    bag.was_delivered = True
    bag.save()

    build_service_stock_scheduler_from_subscription.delay(subscription.id)

    logger.info(f'Subscription was created with id {subscription.id}')


@shared_task(bind=True, base=BaseTaskWithRetry)
def build_plan_financing(self, bag_id: int, invoice_id: int):
    logger.info(f'Starting build_plan_financing for bag {bag_id}')

    if not (bag := Bag.objects.filter(id=bag_id, status='PAID', was_delivered=False).first()):
        logger.error(f'Bag with id {bag_id} not found')
        return

    if not (invoice := Invoice.objects.filter(id=invoice_id, status='FULFILLED').first()):
        logger.error(f'Invoice with id {invoice_id} not found')
        return

    if not invoice.amount:
        logger.error(f'An invoice without amount is prohibited (id: {invoice_id})')
        return

    utc_now = timezone.now()
    months = bag.how_many_installments
    plans = bag.plans.all()
    delta = relativedelta(0)

    for plan in plans:
        unit = plan.time_of_life
        unit_type = plan.time_of_life_unit

        if not unit or not unit_type:
            continue

        new_delta = actions.calculate_relative_delta(unit, unit_type)
        if utc_now + new_delta > utc_now + delta:
            delta = new_delta

    cohort = bag.selected_cohorts.first()
    plan = bag.plans.first()

    event_type_set = bag.selected_event_type_sets.first()
    if plan and not event_type_set:
        event_type_set = plan.event_type_set

    mentorship_service_set = bag.selected_mentorship_service_sets.first()
    if plan and not mentorship_service_set:
        mentorship_service_set = plan.mentorship_service_set

    financing = PlanFinancing.objects.create(user=bag.user,
                                             next_payment_at=invoice.paid_at + relativedelta(months=1),
                                             academy=bag.academy,
                                             selected_cohort=cohort,
                                             selected_event_type_set=event_type_set,
                                             selected_mentorship_service_set=mentorship_service_set,
                                             valid_until=invoice.paid_at + relativedelta(months=months),
                                             plan_expires_at=invoice.paid_at + delta,
                                             monthly_price=invoice.amount,
                                             status='ACTIVE')

    financing.plans.set(plans)

    financing.save()
    financing.invoices.add(invoice)

    bag.was_delivered = True
    bag.save()

    build_service_stock_scheduler_from_plan_financing.delay(financing.id)

    logger.info(f'PlanFinancing was created with id {financing.id}')


@shared_task(bind=True, base=BaseTaskWithRetry)
def build_free_subscription(self, bag_id: int, invoice_id: int):
    logger.info(f'Starting build_free_subscription for bag {bag_id}')

    if not (bag := Bag.objects.filter(id=bag_id, status='PAID', was_delivered=False).first()):
        logger.error(f'Bag with id {bag_id} not found')
        return

    if not (invoice := Invoice.objects.filter(id=invoice_id, status='FULFILLED').first()):
        logger.error(f'Invoice with id {invoice_id} not found')
        return

    if invoice.amount != 0:
        logger.error(f'The invoice with id {invoice_id} is invalid for a free subscription')
        return

    plans = bag.plans.all()

    if not plans:
        logger.error(f'Not have plans to associated to this free subscription in the bag {bag_id}')
        return

    for plan in plans:
        is_free_trial = True
        unit = plan.trial_duration
        unit_type = plan.trial_duration_unit

        if not unit:
            is_free_trial = False
            unit = plan.time_of_life
            unit_type = plan.time_of_life_unit

        delta = actions.calculate_relative_delta(unit, unit_type)

        until = invoice.paid_at + delta

        cohort = bag.selected_cohorts.first()

        event_type_set = bag.selected_event_type_sets.first()
        if not event_type_set:
            event_type_set = plan.event_type_set

        mentorship_service_set = bag.selected_mentorship_service_sets.first()
        if not mentorship_service_set:
            mentorship_service_set = plan.mentorship_service_set

        status = 'FREE_TRIAL' if is_free_trial else 'ACTIVE'

        if is_free_trial:
            extra = {
                'status': 'FREE_TRIAL',
                'valid_until': until,
            }

        elif not is_free_trial and plan.is_renewable:
            extra = {
                'status': 'ACTIVE',
                'valid_until': None,
            }

        else:
            extra = {
                'status': 'ACTIVE',
                'valid_until': until,
            }

        subscription = Subscription.objects.create(user=bag.user,
                                                   paid_at=invoice.paid_at,
                                                   academy=bag.academy,
                                                   selected_cohort=cohort,
                                                   selected_event_type_set=event_type_set,
                                                   selected_mentorship_service_set=mentorship_service_set,
                                                   next_payment_at=until,
                                                   **extra)

        subscription.plans.add(plan)

        subscription.save()
        subscription.invoices.add(invoice)

        build_service_stock_scheduler_from_subscription.delay(subscription.id)

        logger.info(f'Free subscription was created with id {subscription.id} for plan {plan.id}')

    bag.was_delivered = True
    bag.save()


@shared_task(bind=True, base=BaseTaskWithRetry)
def end_the_consumption_session(self, consumption_session_id: int, how_many: float = 1.0):
    logger.info(f'Starting end_the_consumption_session for ConsumptionSession {consumption_session_id}')

    session = ConsumptionSession.objects.filter(id=consumption_session_id).first()
    if not session:
        logger.error(f'ConsumptionSession with id {consumption_session_id} not found')
        return

    if session.status != 'PENDING':
        logger.error(f'ConsumptionSession with id {consumption_session_id} already processed')
        return

    consumable = session.consumable
    consume_service.send(instance=consumable, sender=consumable.__class__, how_many=how_many)

    session.was_discounted = True
    session.status = 'DONE'
    session.save()


# TODO: this task is not being used, if you will use this task, you need to take in consideration
# you need fix the logic about the consumable valid until, maybe this must be removed
@shared_task(bind=False, base=BaseTaskWithRetry)
def build_consumables_from_bag(bag_id: int):
    logger.info(f'Starting build_consumables_from_bag for bag {bag_id}')

    if not (bag := Bag.objects.filter(id=bag_id, status='PAID', was_delivered=False).first()):
        logger.error(f'Bag with id {bag_id} not found')
        return

    mentorship_service_set = bag.selected_mentorship_service_sets.first()
    event_type_set = bag.selected_event_type_sets.first()

    if [mentorship_service_set, event_type_set].count(None) != 1:
        logger.error(f'Bag with id {bag_id} not have a resource associated')
        return

    consumables = []
    for service_item in bag.service_items.all():
        kwargs = {}
        if service_item.service_item_type == 'MENTORSHIP_SERVICE_SET':
            kwargs['mentorship_service_set'] = mentorship_service_set

        if service_item.service_item_type == 'EVENT_TYPE_SET':
            kwargs['event_type_set'] = event_type_set

        if not kwargs:
            logger.error(f'Bag with id {bag_id} have a resource associated opposite to the service item type')
            return

        consumables.append(
            Consumable(service_item=service_item,
                       unit_type=service_item.unit_type,
                       how_many=service_item.how_many,
                       user=bag.user,
                       **kwargs))

    for consumable in consumables:
        consumable.save()

    bag.was_delivered = True
    bag.save()


@shared_task(bind=False, base=BaseTaskWithRetry)
def refund_mentoring_session(session_id: int):
    from breathecode.mentorship.models import MentorshipSession

    logger.info(f'Starting refund_mentoring_session for mentoring session {session_id}')

    if not (mentorship_session := MentorshipSession.objects.filter(
            id=session_id, mentee__isnull=False, service__isnull=False, status__in=['FAILED', 'IGNORED'
                                                                                    ]).first()):
        logger.error(f'MentoringSession with id {session_id} not found or is invalid')
        return

    mentee = mentorship_session.mentee
    service = mentorship_session.service

    consumption_session = ConsumptionSession.objects.filter(
        consumable__user=mentee,
        consumable__mentorship_service_set__mentorship_services=service).exclude(status='CANCELLED').first()

    if not consumption_session:
        logger.error(f'ConsumptionSession not found for mentorship session {session_id}')
        return

    if consumption_session.status == 'CANCELLED':
        logger.error(f'ConsumptionSession already cancelled for mentorship session {session_id}')
        return

    consumption_session.status == 'CANCELLED'
    consumption_session.save()

    if consumption_session.status == 'DONE':
        logger.info('Refunding consumption session because it was discounted')

        how_many = consumption_session.how_many
        consumable = consumption_session.consumable
        reimburse_service_units.send(instance=consumable, sender=consumable.__class__, how_many=how_many)
        return
