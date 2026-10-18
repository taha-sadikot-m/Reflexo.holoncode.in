// Default layer (BETA_04 §7.3): what happens when no owner rule covers a request.
// routine acts; commitment and money propose; a draft order is not a promise yet.

@id("def.draft")
@effect("allow")
permit (
  principal,
  action in [Reflexo::Action::"order.cancel", Reflexo::Action::"order.change"],
  resource is Reflexo::Order
) when { resource.status == "draft" };

@id("def.routine")
@effect("allow")
permit (principal, action in Reflexo::Action::"routine", resource);

@id("def.commitment")
@effect("propose")
permit (principal, action in Reflexo::Action::"commitment", resource)
  unless {
    action in [Reflexo::Action::"order.cancel", Reflexo::Action::"order.change"]
    && resource is Reflexo::Order && resource.status == "draft"
  };

@id("def.money")
@effect("propose")
permit (principal, action in Reflexo::Action::"money", resource);
