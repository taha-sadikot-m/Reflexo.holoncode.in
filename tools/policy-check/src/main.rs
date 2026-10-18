//! reflexo-policy-check: verifies Reflexo's Cedar policy sets before a rule goes live (BETA_04 §7.6).
//!
//! Usage: `reflexo-policy-check <compare|lint|never-errors|implies>` with one JSON object on stdin:
//!   compare       {"schema", "pairs": [{"name", "old", "new"}]}
//!   lint          {"schema", "set"}
//!   never-errors  {"schema", "set"}
//!   implies       {"schema", "set", "property"}
//! and one JSON object on stdout. Exit 0 with {"error": ...} when an input is invalid; non-zero only when the
//! checker itself cannot run (no solver, unreadable stdin).
//!
//! Every question is asked per request environment of the schema (principal type, action, resource type) with
//! `cedar-policy-symcc`, whose core is verified in Lean, using the cvc5 solver named by `CVC5`.

use std::collections::{BTreeMap, BTreeSet};
use std::io::Read;
use std::str::FromStr;

use cedar_policy::{ActionConstraint, Effect, EntityUid, Policy, PolicySet, RequestEnv, Schema};
use cedar_policy_symcc::solver::LocalSolver;
use cedar_policy_symcc::{CedarSymCompiler, CompiledPolicy, CompiledPolicySet, Env};
use serde::Deserialize;
use serde_json::{json, Value};

const MAX_EXAMPLES: usize = 3;

#[derive(Deserialize)]
struct Pair {
    name: String,
    old: String,
    new: String,
}

#[derive(Deserialize)]
struct Input {
    schema: String,
    #[serde(default)]
    pairs: Vec<Pair>,
    #[serde(default)]
    set: String,
    #[serde(default)]
    property: String,
}

type Compiler = CedarSymCompiler<LocalSolver>;

struct Fail(String);

impl<E: std::fmt::Display> From<E> for Fail {
    fn from(e: E) -> Self {
        Fail(e.to_string())
    }
}

fn parse_set(text: &str, what: &str) -> Result<PolicySet, Fail> {
    PolicySet::from_str(text).map_err(|e| Fail(format!("{what} does not parse: {e}")))
}

/// The policy's `@id`, else Cedar's positional id.
fn pid(p: &Policy) -> String {
    p.annotation("id").map(str::to_string).unwrap_or_else(|| p.id().to_string())
}

fn safety(p: &Policy) -> u8 {
    match (p.effect(), p.annotation("effect")) {
        (Effect::Forbid, _) => 3,
        (Effect::Permit, Some("propose")) => 2,
        (Effect::Permit, Some("shadow")) => 2,
        _ => 1,
    }
}

/// The actions a policy can apply to; None = any.
fn actions_of(p: &Policy) -> Option<BTreeSet<String>> {
    match p.action_constraint() {
        ActionConstraint::Any => None,
        ActionConstraint::Eq(uid) => Some(BTreeSet::from([uid.to_string()])),
        ActionConstraint::In(uids) => {
            // groups ("routine") are not resolved here: treat as any
            if uids.iter().any(|u| !u.to_string().contains('.')) {
                None
            } else {
                Some(uids.iter().map(EntityUid::to_string).collect())
            }
        }
    }
}

fn applies(p: &Policy, env: &RequestEnv) -> bool {
    actions_of(p).is_none_or(|acts| acts.contains(&env.action().to_string()))
}

/// Only environments some permit can apply to can allow anything: the others need no solver call.
fn permit_envs(schema: &Schema, sets: &[&PolicySet]) -> Vec<RequestEnv> {
    schema
        .request_envs()
        .filter(|env| {
            sets.iter()
                .any(|s| s.policies().any(|p| p.effect() == Effect::Permit && applies(p, env)))
        })
        .collect()
}

fn example(env: &Env) -> Value {
    let r = &env.request;
    json!({
        "principal": r.principal().map(|u| u.to_string()).unwrap_or_default(),
        "action": r.action().map(|u| u.to_string()).unwrap_or_default(),
        "resource": r.resource().map(|u| u.to_string()).unwrap_or_default(),
        "context": r.context().and_then(|c| c.to_json_value().ok()).unwrap_or(json!({})),
        "entities": env.entities.to_json_value().unwrap_or(json!([])),
    })
}

fn compile_set(set: &PolicySet, env: &RequestEnv, schema: &Schema) -> Result<CompiledPolicySet, Fail> {
    CompiledPolicySet::compile(set, env, schema).map_err(|e| Fail(format!("{}: {e}", env.action())))
}

async fn compare(c: &mut Compiler, schema: &Schema, input: &Input) -> Result<Value, Fail> {
    let mut out = Vec::new();
    for pair in &input.pairs {
        let old = parse_set(&pair.old, "old")?;
        let new = parse_set(&pair.new, "new")?;
        let (mut more, mut less) = (Vec::new(), Vec::new());
        for env in permit_envs(schema, &[&old, &new]) {
            let o = compile_set(&old, &env, schema)?;
            let n = compile_set(&new, &env, schema)?;
            if more.len() < MAX_EXAMPLES {
                if let Some(cex) = c.check_implies_with_counterexample_opt(&n, &o).await? {
                    more.push(example(&cex));
                }
            }
            if less.len() < MAX_EXAMPLES {
                if let Some(cex) = c.check_implies_with_counterexample_opt(&o, &n).await? {
                    less.push(example(&cex));
                }
            }
        }
        let relation = match (more.is_empty(), less.is_empty()) {
            (true, true) => "equivalent",
            (false, true) => "more_permissive",
            (true, false) => "less_permissive",
            (false, false) => "incomparable",
        };
        out.push(json!({"name": pair.name, "relation": relation, "more": more, "less": less}));
    }
    Ok(json!({ "comparisons": out }))
}

async fn lint(c: &mut Compiler, schema: &Schema, input: &Input) -> Result<Value, Fail> {
    let set = parse_set(&input.set, "set")?;
    let policies: Vec<&Policy> = set.policies().collect();
    let envs: Vec<RequestEnv> = schema.request_envs().collect();
    let mut findings = Vec::new();
    for (i, p) in policies.iter().enumerate() {
        let mine: Vec<&RequestEnv> = envs.iter().filter(|e| applies(p, e)).collect();
        let mut can_match = false;
        let mut always = !mine.is_empty();
        let mut shadow: BTreeMap<usize, bool> = BTreeMap::new(); // other policy → implies p in every env so far
        for env in &mine {
            let cp = CompiledPolicy::compile(p, env, schema)?;
            let never = c.check_never_matches_opt(&cp).await?;
            if !never {
                can_match = true;
                if p.effect() == Effect::Forbid && !c.check_always_matches_opt(&cp).await? {
                    always = false;
                }
            }
            for (j, q) in policies.iter().enumerate() {
                if j == i || safety(q) < safety(p) || (safety(q) == safety(p) && j > i) {
                    continue;
                }
                if *shadow.get(&j).unwrap_or(&true) {
                    let covers = if never || !applies(q, env) {
                        never
                    } else {
                        let cq = CompiledPolicy::compile(q, env, schema)?;
                        c.check_matches_implies_opt(&cp, &cq).await?
                    };
                    shadow.insert(j, covers);
                }
            }
        }
        if !can_match {
            findings.push(json!({"policy_id": pid(p), "kind": "impossible"}));
            continue;
        }
        if let Some((j, _)) = shadow.iter().find(|(_, covers)| **covers) {
            findings.push(json!({"policy_id": pid(p), "kind": "shadowed", "by": pid(policies[*j])}));
        }
        if p.effect() == Effect::Forbid && always {
            findings.push(json!({"policy_id": pid(p), "kind": "overrides_all"}));
        }
    }
    Ok(json!({ "findings": findings }))
}

async fn never_errors(c: &mut Compiler, schema: &Schema, input: &Input) -> Result<Value, Fail> {
    let set = parse_set(&input.set, "set")?;
    let mut may_error = BTreeSet::new();
    for env in schema.request_envs() {
        for p in set.policies().filter(|p| applies(p, &env)) {
            let cp = CompiledPolicy::compile(p, &env, schema)?;
            if !c.check_never_errors_opt(&cp).await? {
                may_error.insert(pid(p));
            }
        }
    }
    Ok(json!({ "may_error": may_error }))
}

async fn implies(c: &mut Compiler, schema: &Schema, input: &Input) -> Result<Value, Fail> {
    let set = parse_set(&input.set, "set")?;
    let property = parse_set(&input.property, "property")?;
    let mut examples = Vec::new();
    for env in permit_envs(schema, &[&set]) {
        let s = compile_set(&set, &env, schema)?;
        let p = compile_set(&property, &env, schema)?;
        if let Some(cex) = c.check_implies_with_counterexample_opt(&s, &p).await? {
            examples.push(example(&cex));
            if examples.len() >= MAX_EXAMPLES {
                break;
            }
        }
    }
    Ok(json!({ "holds": examples.is_empty(), "counterexamples": examples }))
}

#[tokio::main(flavor = "current_thread")]
async fn main() {
    let command = std::env::args().nth(1).unwrap_or_default();
    let mut raw = String::new();
    if std::io::stdin().read_to_string(&mut raw).is_err() {
        eprintln!("cannot read stdin");
        std::process::exit(2);
    }
    let input: Input = match serde_json::from_str(&raw) {
        Ok(i) => i,
        Err(e) => {
            println!("{}", json!({ "error": format!("input is not valid JSON: {e}") }));
            return;
        }
    };
    let schema = match Schema::from_cedarschema_str(&input.schema) {
        Ok((s, _warnings)) => s,
        Err(e) => {
            println!("{}", json!({ "error": format!("schema does not parse: {e}") }));
            return;
        }
    };
    let solver = match LocalSolver::cvc5() {
        Ok(s) => s,
        Err(e) => {
            eprintln!("cannot start cvc5: {e}");
            std::process::exit(3);
        }
    };
    let mut compiler = match CedarSymCompiler::new(solver) {
        Ok(c) => c,
        Err(e) => {
            eprintln!("cannot start the symbolic compiler: {e}");
            std::process::exit(3);
        }
    };
    let result = match command.as_str() {
        "compare" => compare(&mut compiler, &schema, &input).await,
        "lint" => lint(&mut compiler, &schema, &input).await,
        "never-errors" => never_errors(&mut compiler, &schema, &input).await,
        "implies" => implies(&mut compiler, &schema, &input).await,
        other => Err(Fail(format!("unknown command '{other}'"))),
    };
    match result {
        Ok(v) => println!("{v}"),
        Err(Fail(msg)) => println!("{}", json!({ "error": msg })),
    }
}
