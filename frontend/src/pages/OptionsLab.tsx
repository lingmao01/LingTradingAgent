import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { CandlestickChart, Loader2 } from "lucide-react";
import { cn } from "@/lib/utils";
import { api } from "@/lib/api";
import {
  buildPayoffRequest,
  buildPresetLegs,
  DEFAULT_PARAMS,
  type OptionLeg,
  type OptionsLabParams,
  type OptionsPayoffRequest,
  type OptionsPayoffResponse,
} from "@/lib/options";
import { StrategyBuilder } from "@/components/options/StrategyBuilder";
import { GreeksCards } from "@/components/options/GreeksCards";
import { OptionsChainTable } from "@/components/options/OptionsChainTable";
import { OptionsPayoffChart } from "@/components/charts/OptionsPayoffChart";
import { OptionsScenarioMatrix } from "@/components/charts/OptionsScenarioMatrix";

const ANALYZE_DEBOUNCE_MS = 500;

function MetricCard({
  label,
  value,
  badge,
  badgeTone = "neutral",
  valueTone = "neutral",
}: {
  label: string;
  value: string;
  badge?: string;
  badgeTone?: "debit" | "credit" | "neutral";
  valueTone?: "positive" | "negative" | "neutral";
}) {
  const badgeCls =
    badgeTone === "debit"
      ? "bg-danger/10 text-danger"
      : badgeTone === "credit"
        ? "bg-success/10 text-success"
        : "bg-muted text-muted-foreground";
  const valueCls =
    valueTone === "positive" ? "text-success" : valueTone === "negative" ? "text-danger" : "";
  return (
    <div className="rounded-xl border border-border/60 bg-card p-4 shadow-sm">
      <div className="flex items-center justify-between gap-2">
        <div className="text-xs text-muted-foreground">{label}</div>
        {badge && (
          <span className={cn("rounded px-1.5 py-0.5 text-[10px] font-medium", badgeCls)}>{badge}</span>
        )}
      </div>
      <div className={cn("mt-1 truncate text-sm font-semibold tabular-nums", valueCls)}>{value}</div>
    </div>
  );
}

export function OptionsLab() {
  const { t } = useTranslation();

  const [params, setParams] = useState<OptionsLabParams>(DEFAULT_PARAMS);
  const [legs, setLegs] = useState<OptionLeg[]>(
    () => buildPresetLegs("long_call", DEFAULT_PARAMS.entry_spot) ?? [],
  );
  const [activePresetId, setActivePresetId] = useState<string | null>("long_call");
  const [result, setResult] = useState<OptionsPayoffResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const generation = useRef(0);

  const analyze = useCallback(
    async (req: OptionsPayoffRequest) => {
      const gen = ++generation.current;
      setLoading(true);
      setError(null);
      try {
        const res = await api.analyzeOptionsPayoff(req);
        if (generation.current === gen) setResult(res);
      } catch (e) {
        if (generation.current === gen) {
          setError(e instanceof Error ? e.message : t("options.errorGeneric"));
        }
      } finally {
        if (generation.current === gen) setLoading(false);
      }
    },
    [t],
  );

  useEffect(() => {
    const req = buildPayoffRequest(legs, params);
    if (!req) return;
    const timer = setTimeout(() => void analyze(req), ANALYZE_DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [legs, params, analyze]);

  const applyPreset = (presetId: string) => {
    const presetLegs = buildPresetLegs(presetId, params.entry_spot);
    if (!presetLegs) return;
    setActivePresetId(presetId);
    setLegs(presetLegs);
  };

  const handleLegsChange = (next: OptionLeg[]) => {
    setActivePresetId(null);
    setLegs(next);
  };

  const summary = result?.summary ?? null;
  const dash = "–";

  const breakevensText = summary
    ? summary.breakevens.length > 0
      ? summary.breakevens.map((be) => be.toLocaleString()).join(", ")
      : t("options.metrics.none")
    : dash;

  const maxProfitText = summary
    ? summary.profit_unbounded
      ? t("options.metrics.unlimited")
      : summary.max_profit !== null
        ? summary.max_profit.toLocaleString(undefined, { maximumFractionDigits: 2 })
        : t("options.metrics.none")
    : dash;

  const maxLossText = summary
    ? summary.loss_unbounded
      ? t("options.metrics.unlimited")
      : summary.max_loss !== null
        ? summary.max_loss.toLocaleString(undefined, { maximumFractionDigits: 2 })
        : t("options.metrics.none")
    : dash;

  return (
    <div className="flex w-full flex-col gap-4 p-6">
      {/* Header */}
      <div>
        <div className="flex items-center gap-3">
          <CandlestickChart className="h-6 w-6 text-primary" />
          <h1 className="text-2xl font-bold">{t("options.title")}</h1>
        </div>
        <p className="mt-1 text-sm text-muted-foreground">{t("options.subtitle")}</p>
      </div>

      {/* Builder + results */}
      <div className="grid gap-4 xl:grid-cols-3">
        <div className="xl:col-span-1">
          <StrategyBuilder
            legs={legs}
            params={params}
            activePresetId={activePresetId}
            onApplyPreset={applyPreset}
            onLegsChange={handleLegsChange}
            onParamsChange={setParams}
          />
        </div>

        <div className="flex flex-col gap-4 xl:col-span-2">
          {/* Key-metrics strip */}
          <div className={cn("grid grid-cols-2 gap-3 lg:grid-cols-4", loading && "opacity-60")}>
            <MetricCard
              label={t("options.metrics.entryCost")}
              value={
                summary
                  ? summary.entry_cost.toLocaleString(undefined, { maximumFractionDigits: 2 })
                  : dash
              }
              badge={summary ? t(`options.metrics.${summary.entry_side}`) : undefined}
              badgeTone={
                summary ? (summary.entry_side === "flat" ? "neutral" : summary.entry_side) : "neutral"
              }
            />
            <MetricCard label={t("options.metrics.breakevens")} value={breakevensText} />
            <MetricCard
              label={t("options.metrics.maxProfit")}
              value={maxProfitText}
              valueTone={summary && !summary.profit_unbounded && (summary.max_profit ?? 0) > 0 ? "positive" : "neutral"}
            />
            <MetricCard
              label={t("options.metrics.maxLoss")}
              value={maxLossText}
              valueTone={summary && !summary.loss_unbounded && (summary.max_loss ?? 0) < 0 ? "negative" : "neutral"}
            />
          </div>

          {/* Backend error banner */}
          {error && (
            <div className="rounded border border-danger/30 bg-danger/5 p-3 text-sm text-danger">{error}</div>
          )}

          {/* Payoff diagram */}
          <section className="rounded-xl border border-border/60 bg-card p-4 shadow-sm">
            <div className="mb-2 flex items-center justify-between">
              <div className="text-sm font-semibold">{t("options.payoff.title")}</div>
              {loading && (
                <span className="flex items-center gap-1.5 text-xs text-muted-foreground">
                  <Loader2 className="h-3.5 w-3.5 animate-spin" />
                  {t("options.builder.analyzing")}
                </span>
              )}
            </div>
            <OptionsPayoffChart
              curve={result?.expiry_curve ?? { spot: [], pnl: [] }}
              entrySpot={params.entry_spot}
              breakevens={summary?.breakevens ?? []}
            />
          </section>
        </div>
      </div>

      {/* Portfolio Greeks */}
      {result && <GreeksCards greeks={result.greeks} />}

      {/* Spot x IV scenario matrix */}
      <section className="rounded-xl border border-border/60 bg-card p-4 shadow-sm">
        <div className="mb-2 text-sm font-semibold">{t("options.scenario.title")}</div>
        <OptionsScenarioMatrix
          grid={result?.scenario_grid ?? { iv_values: [], spot: [], pnl: [] }}
        />
        {result && result.limitations.length > 0 && (
          <ul className="mt-2 list-disc ps-5 text-xs text-muted-foreground">
            {result.limitations.map((lim, i) => (
              <li key={i}>{lim}</li>
            ))}
          </ul>
        )}
      </section>

      {/* Live US options chain */}
      <OptionsChainTable referenceSpot={params.entry_spot} />

      <p className="pb-2 text-xs text-muted-foreground">{t("options.disclaimer")}</p>
    </div>
  );
}
