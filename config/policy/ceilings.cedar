// Ceiling layer (BETA_04 §7.3): caps an effect. A match turns act into propose, or act and propose into
// shadow (@effect("shadow"), BETA_07 §3.2).

@id("ceil.shadow")
@effect("shadow")
permit (principal, action, resource)
  when {
    context has customer_mode && context.customer_mode == "shadow"
    && ["customer_turn", "voice"].contains(context.requested_by)
    && !(context has unit_granted && context.unit_granted)
  };

@id("ceil.customer_writes")
permit (principal, action in Reflexo::Action::"commitment", resource is Reflexo::ConnectorTool)
  when { context.requested_by == "customer_turn" };

@id("ceil.money")
permit (principal, action in Reflexo::Action::"money", resource)
  when { context.requested_by == "customer_turn" };
