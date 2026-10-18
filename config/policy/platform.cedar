// Platform layer (BETA_04 §7.3): forbid only. Hard invariants and isolation. Reviewed like code.
// A matching forbid makes the request not_possible; @reason is what the customer is told, in the agent's words.

@id("inv.cancel_closed")
@reason("It is already on its way or delivered.")
forbid (principal, action == Reflexo::Action::"order.cancel", resource is Reflexo::Order)
  when { ["out_for_delivery", "delivered"].contains(resource.status) };

@id("inv.change_closed")
@reason("It was cancelled, or is already on its way or delivered.")
forbid (principal, action == Reflexo::Action::"order.change", resource is Reflexo::Order)
  when { ["cancelled", "out_for_delivery", "delivered"].contains(resource.status) };

@id("inv.refund_unpaid")
@reason("Nothing was paid online for this order.")
forbid (principal, action == Reflexo::Action::"refund.request", resource is Reflexo::Order)
  unless { resource.payment_status == "paid" };

@id("iso.business")
@reason("Cross-business access.")
forbid (principal, action, resource) unless { resource in principal.business };
