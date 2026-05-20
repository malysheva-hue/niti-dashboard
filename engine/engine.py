"""PatternEngine — главный анализатор.

Pipeline:
  load_data() → build_peer_groups() → analyze() → dedupe() → prioritize() → top-10

Каждое правило проходит через:
  applies_to(sku, context) → triggers_match(sku, context) →
  evaluate_diagnostics() → lookup_action() → Finding(...)

Идемпотентность: random.seed(today) перед сборкой выходного JSON
(seed ставится снаружи в update.py).
"""
from __future__ import annotations

import statistics
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from . import contexts as ctx
from .operators import OPERATORS, OpResult
from .rule_loader import Rule, load_rules


# ============================================================
# МОДЕЛИ
# ============================================================
@dataclass
class Finding:
    sku_code: str
    manager: str
    rule_id: str
    severity_score: int
    diagnostics: list[str] = field(default_factory=list)
    diagnostics_details: dict = field(default_factory=dict)
    action_key: str = "multiple_issues"
    task_type: str = "risk"           # risk | opportunity
    priority: str = "yellow"          # red | yellow
    sku_data: dict = field(default_factory=dict)
    trigger_details: dict = field(default_factory=dict)


# ============================================================
# СБОРКА ВХОДНЫХ ДАННЫХ
# ============================================================
def assemble_sku_records(data: dict, current_month: str) -> list[dict]:
    """Объединяет sku_history (30 дней) и месячный срез в один dict-на-SKU.

    Возвращает [{code, name, category, group, manager, month: {...}, history: {...}}].
    """
    history = data.get("sku_history", {}) or {}
    months_data = data.get("_months_data", {}) or {}
    month_rows = months_data.get(current_month, []) or []
    by_code_month = {r["code"]: r for r in month_rows if r.get("code")}

    out = []
    # Сначала по тем, у кого есть history (top-100 + звёзды/локо)
    for code, h in history.items():
        m = by_code_month.get(code, {})
        out.append({
            "code": code,
            "name": m.get("name"),
            "category": m.get("category"),
            "group": m.get("group"),
            "manager": m.get("manager"),
            "month": m,
            "history": h,
        })
    # Плюс остальные SKU из месячного среза (для правил, которые не требуют history)
    for code, m in by_code_month.items():
        if code in history:
            continue
        out.append({
            "code": code,
            "name": m.get("name"),
            "category": m.get("category"),
            "group": m.get("group"),
            "manager": m.get("manager"),
            "month": m,
            "history": {},   # пустая история — trend-операторы вернут EMPTY
        })
    return out


def build_peer_groups(sku_records: list[dict]) -> dict:
    """Группирует SKU по (категория, роль) → списки числовых метрик.

    {(category, role): {"margin": [...], "drr": [...], "tacos": [...]}}
    """
    groups = {}
    for s in sku_records:
        cat = s.get("category") or "—"
        role = ctx.role_of(s.get("group", "") or "")
        if not cat or not role:
            continue
        key = (cat, role)
        g = groups.setdefault(key, {"margin": [], "drr": [], "tacos": []})
        m = s.get("month") or {}
        if m.get("margin") is not None:
            g["margin"].append(m["margin"])
        if m.get("drr") is not None:
            g["drr"].append(m["drr"])
        if m.get("tacos") is not None:
            g["tacos"].append(m["tacos"])
    return groups


# ============================================================
# PATTERN ENGINE
# ============================================================
class PatternEngine:
    def __init__(self, rules_dir: str | Path, verbose: bool = True):
        self.rules: list[Rule] = load_rules(rules_dir)
        self.operators = OPERATORS
        self.contexts = ctx
        self.verbose = verbose
        self.peer_groups: dict = {}
        # счётчики для диагностики
        self.rule_match_counts: dict[str, int] = {r.id: 0 for r in self.rules}
        self.skipped: int = 0
        self.skipped_reasons: dict[str, int] = {}

    # --------------------------------------------------------
    # applies_to
    # --------------------------------------------------------
    def _applies(self, rule: Rule, sku: dict) -> tuple[bool, str]:
        ap = rule.applies_to or {}
        code = sku.get("code")
        if not code:
            return False, "no_code"

        # require_active
        if ap.get("require_active"):
            orders30 = sum((v or 0) for v in (sku.get("history", {}).get("orders") or []))
            month_orders = (sku.get("month") or {}).get("orders") or 0
            if orders30 < 5 and month_orders < 5:
                return False, "not_active"

        # stars_only / loco_risk_only
        if ap.get("stars_only") and code not in ctx.STARS_CODES:
            return False, "not_star"
        if ap.get("loco_risk_only") and code not in ctx.LOCO_RISK_CODES:
            return False, "not_loco_risk"
        if ap.get("exclude_stars") and code in ctx.STARS_CODES:
            return False, "is_star"
        if ap.get("exclude_loco_risk") and code in ctx.LOCO_RISK_CODES:
            return False, "is_loco_risk"

        # roles
        if "roles" in ap:
            role = ctx.role_of(sku.get("group", "") or "")
            if role not in ap["roles"]:
                return False, f"role_not_in_{ap['roles']}"

        # seasonal_only
        if ap.get("seasonal_only") and not ctx.is_seasonal_category(sku.get("category") or ""):
            return False, "not_seasonal"

        return True, ""

    # --------------------------------------------------------
    # Подготовка серии для оператора (metric → values)
    # --------------------------------------------------------
    def _series_for(self, sku: dict, metric: str):
        """Извлекает серию из sku по имени метрики."""
        h = sku.get("history", {}) or {}
        m = sku.get("month", {}) or {}
        # история по метрике
        if metric in ("orders", "revenue", "stock", "spp", "price"):
            return h.get(metric) or []
        # месячный одиночный показатель (последнее значение)
        if metric in ("margin", "drr", "tacos", "turnover"):
            return m.get(metric)
        if metric == "category":
            return sku.get("category")
        return None

    # --------------------------------------------------------
    # Запуск оператора с конфигом из YAML
    # --------------------------------------------------------
    def _run_op(self, op_cfg: dict, sku: dict) -> OpResult:
        name = op_cfg.get("operator")
        fn = self.operators.get(name)
        if not fn:
            return OpResult(False, 0, {"error": f"unknown operator {name}"})

        metric = op_cfg.get("metric")
        series = self._series_for(sku, metric) if metric else None

        try:
            if name == "trend":
                return fn(series,
                          window_days=op_cfg.get("window_days", 7),
                          direction=op_cfg.get("direction", "down"),
                          min_change_pct=op_cfg.get("min_change_pct", -30))
            if name == "price_jump_detected":
                return fn(series,
                          window_days=op_cfg.get("window_days", 7),
                          jump_pct=op_cfg.get("jump_pct", 10))
            if name == "spp_drop_detected":
                return fn(series,
                          window_days=op_cfg.get("window_days", 7),
                          threshold_pp=op_cfg.get("threshold_pp", 5))
            if name == "days_to_zero_with_safety":
                # velocity всегда из заказов, не из той серии что в metric
                stock = (sku.get("month") or {}).get("stock")
                orders = (sku.get("history") or {}).get("orders") or []
                clean = [v for v in orders[-7:] if v is not None]
                velocity = sum(clean) / len(clean) if clean else 0
                return fn(stock, velocity, safety_days=op_cfg.get("safety_days", 7))
            if name == "threshold":
                # series может быть числом (margin/drr/turnover) или None
                val = series if not isinstance(series, list) else None
                return fn(val, min=op_cfg.get("min"), max=op_cfg.get("max"))
            if name == "threshold_by_role":
                val = series if not isinstance(series, list) else None
                role = ctx.role_of(sku.get("group", "") or "")
                # multiplier для DRR_RUNAWAY
                mult = op_cfg.get("multiplier")
                if mult and metric == "drr":
                    norms = ctx.ROLE_NORMS.get(role, {})
                    max_ = (norms.get("drr_max") or 0) * mult
                    return self.operators["threshold"](val, max=max_)
                return fn(val, metric=metric, role=role)
            if name == "vs_peer_group":
                cat = sku.get("category") or "—"
                role = ctx.role_of(sku.get("group", "") or "")
                peers = self.peer_groups.get((cat, role), {}).get(metric, [])
                val = (sku.get("month") or {}).get(metric)
                return fn(val, peers, deviation_pct=op_cfg.get("deviation_pct", 20))
            if name == "is_seasonal":
                return fn(sku.get("category") or "")
            if name == "is_in_stars_list":
                return fn(sku.get("code") or "")
            if name == "is_in_loco_risk_list":
                return fn(sku.get("code") or "")
            if name == "is_active_sku":
                return fn(sku)
            if name in ("moving_average", "acceleration", "volatility"):
                return fn(series or [])
            if name == "compare_periods":
                return fn(series or [],
                          period_a_days=op_cfg.get("period_a_days", 7),
                          period_b_days=op_cfg.get("period_b_days", 7))
            if name == "promo_started":
                return fn(series or [],
                          window_days=op_cfg.get("window_days", 7),
                          drop_pct=op_cfg.get("drop_pct", 15))
            if name == "promo_ended":
                return fn(series or [],
                          window_days=op_cfg.get("window_days", 7),
                          recovery_pct=op_cfg.get("recovery_pct", 10))
            if name == "overstocked":
                stock = (sku.get("month") or {}).get("stock")
                orders = (sku.get("history") or {}).get("orders") or []
                clean = [v for v in orders[-7:] if v is not None]
                velocity = sum(clean) / len(clean) if clean else 0
                return fn(stock, velocity, threshold_days=op_cfg.get("threshold_days", 120))
            return OpResult(False, 0, {"error": f"no handler for {name}"})
        except Exception as e:
            return OpResult(False, 0, {"error": f"{type(e).__name__}: {e}"})

    # --------------------------------------------------------
    # Триггер
    # --------------------------------------------------------
    def _check_triggers(self, rule: Rule, sku: dict) -> tuple[bool, dict]:
        """Проверяет триггеры. По умолчанию AND, но 'or_trigger: true' переключает в OR.

        Возвращает (matched, trigger_details).
        """
        if not rule.triggers:
            return False, {}
        has_or = any(t.get("or_trigger") for t in rule.triggers)
        results = []
        for t in rule.triggers:
            r = self._run_op(t, sku)
            results.append((t, r))
        if has_or:
            matched = any(r.matched for _, r in results)
        else:
            matched = all(r.matched for _, r in results)
        if not matched:
            return False, {}
        trig_details = {
            t.get("metric", t.get("operator")): r.details
            for t, r in results if r.matched
        }
        return True, trig_details

    # --------------------------------------------------------
    # Диагностика — собираем дополнительные симптомы и score
    # --------------------------------------------------------
    def _evaluate_diagnostics(self, rule: Rule, sku: dict) -> tuple[int, list[str], dict]:
        bonus = 0
        labels = []
        details = {}
        for d in rule.diagnostics:
            r = self._run_op(d, sku)
            if r.matched:
                bonus += int(d.get("add_score", 0))
                if d.get("label"):
                    labels.append(d["label"])
                    details[d["label"]] = r.details
        return bonus, labels, details

    # --------------------------------------------------------
    # Выбор action из action_lookup
    # --------------------------------------------------------
    def _lookup_action(self, rule: Rule, sku: dict, diag_labels: list[str], diag_details: dict) -> str:
        """Резолвинг action из action_lookup: default + список overrides.

        Формат YAML:
          action_lookup:
            default: rule.fallback
            overrides:
              - if_diagnostics_include: [spp_lost]
                action: rule.spp_recovery
              - if_role_is: [Локомотив]
                action: rule.lokomotiv
              - if_stock_zero: true
                action: rule.oos
              - if_margin_above: 25
                action: rule.high_margin
        """
        al = rule.action_lookup or {}
        default = al.get("default", "multiple_issues")
        overrides = al.get("overrides") or []
        if not isinstance(overrides, list):
            return default

        role = ctx.role_of(sku.get("group", "") or "")
        stock = (sku.get("month") or {}).get("stock")
        margin = (sku.get("month") or {}).get("margin")

        for ov in overrides:
            if not isinstance(ov, dict) or "action" not in ov:
                continue
            ok = True
            if "if_diagnostics_include" in ov:
                want = ov["if_diagnostics_include"] or []
                if not isinstance(want, list):
                    want = [want]
                if not any(lk in diag_labels for lk in want):
                    ok = False
            if ok and "if_role_is" in ov:
                want = ov["if_role_is"] or []
                if not isinstance(want, list):
                    want = [want]
                if role not in want:
                    ok = False
            if ok and ov.get("if_stock_zero"):
                if stock != 0:
                    ok = False
            if ok and "if_margin_above" in ov:
                try:
                    if (margin or 0) <= float(ov["if_margin_above"]):
                        ok = False
                except (TypeError, ValueError):
                    ok = False
            if ok and "if_margin_below" in ov:
                try:
                    if (margin or 999) >= float(ov["if_margin_below"]):
                        ok = False
                except (TypeError, ValueError):
                    ok = False
            if ok:
                return ov["action"]
        return default

    # --------------------------------------------------------
    # ANALYZE
    # --------------------------------------------------------
    def analyze(self, sku_records: list[dict]) -> list[Finding]:
        self.peer_groups = build_peer_groups(sku_records)
        findings = []

        for sku in sku_records:
            for rule in self.rules:
                ok, reason = self._applies(rule, sku)
                if not ok:
                    continue
                triggered, trig_details = self._check_triggers(rule, sku)
                if not triggered:
                    continue

                bonus, diag_labels, diag_details = self._evaluate_diagnostics(rule, sku)
                # v1.1: убран clamp, шкала растянута до 30..180
                severity = rule.severity_base + bonus + ctx.boost_for_sku(sku["code"])

                # v1.1: priority пороги — red >=120, yellow >=70, иначе отсев
                task_type = "opportunity" if rule.raw.get("task_type") == "opportunity" else "risk"
                if task_type == "opportunity":
                    priority = "yellow"
                elif severity >= 120:
                    priority = "red"
                elif severity >= 70:
                    priority = "yellow"
                else:
                    continue  # отсев — слишком слабый сигнал

                action_key = self._lookup_action(rule, sku, diag_labels, diag_details)

                # подцепляем trigger_details в diagnostics_details
                merged_details = {**trig_details, **diag_details}

                f = Finding(
                    sku_code=sku["code"],
                    manager=sku.get("manager") or "—",
                    rule_id=rule.id,
                    severity_score=int(severity),
                    diagnostics=diag_labels,
                    diagnostics_details=merged_details,
                    action_key=action_key,
                    task_type=task_type,
                    priority=priority,
                    sku_data=sku,
                    trigger_details=trig_details,
                )
                findings.append(f)
                self.rule_match_counts[rule.id] += 1

        return findings

    # --------------------------------------------------------
    # Дедупликация — один SKU оставляем с самым высоким severity
    # --------------------------------------------------------
    def dedupe(self, findings: list[Finding]) -> list[Finding]:
        best: dict[str, Finding] = {}
        for f in findings:
            cur = best.get(f.sku_code)
            if not cur or f.severity_score > cur.severity_score:
                best[f.sku_code] = f
        return list(best.values())

    # --------------------------------------------------------
    # Приоритизация:
    # 1. severity_score > 85 → попадают все (без балансировки)
    # 2. остальные balanced ≤4-5 на менеджера, общий лимит 10
    # --------------------------------------------------------
    def prioritize(self, findings: list[Finding], total_limit: int = 15,
                   per_manager_limit: int = 7, opportunity_reserve: int = 3,
                   force_star_inject: bool = True) -> list[Finding]:
        """v1.9 приоритизация:
        1. Делим findings на risk и opportunity.
        2. Если force_star_inject — звёзды плана (если есть в findings) ставятся первыми.
        3. Risk сортируем по severity desc, балансируем ≤per_manager_limit.
        4. Резервируем opportunity_reserve слотов.
        """
        risks = sorted([f for f in findings if f.task_type == "risk"],
                       key=lambda x: -x.severity_score)
        opportunities = sorted([f for f in findings if f.task_type == "opportunity"],
                               key=lambda x: -x.severity_score)

        opp_slots = min(opportunity_reserve, len(opportunities))
        risk_limit = total_limit - opp_slots

        # v1.9: звёзды плана всегда впереди (если есть в findings)
        selected_risk: list[Finding] = []
        per_mgr: dict[str, int] = {}
        if force_star_inject:
            star_findings = [f for f in risks if f.sku_code in ctx.STARS_CODES]
            for f in star_findings:
                if f in selected_risk:
                    continue
                selected_risk.append(f)
                per_mgr[f.manager] = per_mgr.get(f.manager, 0) + 1

        for f in risks:
            if f in selected_risk:
                continue
            if len(selected_risk) >= risk_limit:
                break
            cnt = per_mgr.get(f.manager, 0)
            if cnt >= per_manager_limit:
                continue
            selected_risk.append(f)
            per_mgr[f.manager] = cnt + 1

        unused = risk_limit - len(selected_risk)
        if unused > 0 and len(opportunities) > opp_slots:
            opp_slots = min(opp_slots + unused, len(opportunities))

        selected_opp = opportunities[:opp_slots]

        return selected_risk + selected_opp
