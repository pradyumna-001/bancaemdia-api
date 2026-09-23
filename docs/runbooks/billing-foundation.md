# Billing foundation rollout

The migration installs tables, policies and triggers. It leaves `billing_rollout.activated_at`
NULL, creates no public price and changes no current access. Apply the migration first, then
review the data and activate only in an approved release window using the migration/admin role:

```sql
SELECT billing_activate_rollout();
```

The function persists the database clock once and backfills every existing user with seven full
days from that instant. It is safe to call again: the timestamp and existing trial grants remain
unchanged. New `usuarios` inserts after activation receive a trial from their account creation
timestamp, including legitimate inserts that bypass `UsuarioRepo`. User inserts and activation
serialize on the singleton rollout row. Never edit `activated_at` manually.

The application role can read rollout and public prices but cannot activate rollout or write the
catalog. An administrator can create a draft price with a positive amount in centavos, BRL,
MONTHLY or YEARLY frequency, and a half-open validity interval. Publication is a separate
administrative operation. The GiST exclusion constraint prevents overlapping public intervals,
including concurrent publications. Do not publish until the commercial price is approved.
Existing subscriptions retain their `price_id` and the referenced price's immutable terms.

Trial grants live in `assinaturas` permanently. Account anonymization clears provider references
and current commercial terms while retaining a minimal trial tombstone keyed by the retained
account ID. Audit triggers
record changed field names only. The export includes the user's subscription state and trial
dates but omits provider customer and subscription references. The access decision becomes
`READ_ONLY` at the exact trial end unless a valid paid period exists; enforcement at API and
worker boundaries belongs to issue #93.
