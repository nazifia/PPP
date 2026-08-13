"""Token authentication, plus the superuser's tenant switch."""

from datetime import timedelta

from django.utils import timezone
from rest_framework.authentication import TokenAuthentication
from rest_framework.exceptions import AuthenticationFailed

from .models import Organization, User

ACT_AS_HEADER = 'HTTP_X_ACT_AS_ORG'
ACT_AS_FIELD = 'organization'

#: The idle timeout for an account with no organisation of its own, which in
#: practice means a platform superuser.
DEFAULT_IDLE_MINUTES = 30

#: How often the last-seen stamp is written. Every authenticated request would
#: mean a write per read, which this is not worth.
LAST_SEEN_RESOLUTION = timedelta(seconds=60)


class ActAsOrgAuthentication(TokenAuthentication):
    """A superuser works inside one tenant by sending `X-Act-As-Org: <org id>`.

    A write that creates a row from nothing (a department, a product, a member
    of staff) has no existing row to name the tenant, so the same id may travel
    as `organization`: in the payload, or in the query string for a request with
    no body of its own, such as suspending a trading link. Header first, then
    query string, then body.

    The organisation is attached to the user object for this request only.
    While it is set the account stops reading across tenants: every view scopes
    to that organisation and the ordinary hospital/supplier rules apply, so the
    superuser can do exactly what an administrator of that tenant could do.
    Anyone else sending either one is ignored rather than refused.
    """

    def authenticate_credentials(self, key):
        """Retire a token that has gone untouched for the organisation's idle
        timeout, so the app's own countdown is a courtesy rather than the only
        thing standing between a lifted device and the data behind it.
        """
        user, token = super().authenticate_credentials(key)
        org = user.organization  # The account's own tenant, not one it acts as.
        # Login refuses a suspended organisation, but a session opened before
        # the suspension would otherwise run on untouched until it went idle.
        # Read here rather than hooked to the save, so it holds however the
        # organisation was suspended — the API, the admin site or a shell.
        if org is not None and not org.is_active:
            token.delete()
            raise AuthenticationFailed('Your organisation has been suspended.')
        limit = timedelta(
            minutes=org.idle_timeout_minutes if org else DEFAULT_IDLE_MINUTES,
        )
        now = timezone.now()
        last_seen = user.last_seen_at or token.created
        # The stamp is up to one resolution behind the truth, so spend that much
        # in the session's favour rather than ending it early.
        if now - last_seen > limit + LAST_SEEN_RESOLUTION:
            token.delete()
            raise AuthenticationFailed('This session timed out. Sign in again.')
        if now - last_seen > LAST_SEEN_RESOLUTION:
            # update() rather than save(): no signals, no racing another request
            # over the rest of the row.
            User.objects.filter(pk=user.pk).update(last_seen_at=now)
            user.last_seen_at = now
        return user, token

    def authenticate(self, request):
        result = super().authenticate(request)
        if result is None:
            return None
        user, token = result
        if not user.is_superuser:
            return user, token
        org_id = (
            str(request.META.get(ACT_AS_HEADER) or '').strip()
            or str(request.GET.get(ACT_AS_FIELD) or '').strip()
            or self._org_from_body(request)
        )
        if org_id:
            org = Organization.objects.filter(pk=org_id).first() if org_id.isdigit() else None
            if org is None:
                raise AuthenticationFailed(f'No organisation with id {org_id}.')
            user.act_as_org = org
        return user, token

    @staticmethod
    def _org_from_body(request):
        """The tenant a headerless create names in its payload."""
        try:
            data = request.data
        except Exception:
            # A body we cannot parse is the view's problem to report, not ours.
            return ''
        if not hasattr(data, 'get'):
            return ''
        return str(data.get(ACT_AS_FIELD) or '').strip()
