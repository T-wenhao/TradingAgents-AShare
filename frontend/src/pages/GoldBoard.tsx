import {
    Activity,
    BarChart3,
    CheckCircle2,
    Clock3,
    Database,
    Gem,
    Loader2,
    Play,
    RefreshCw,
    ShieldAlert,
    Target,
    Terminal,
    TrendingUp,
} from 'lucide-react'
import { type ReactNode, useCallback, useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'

import { api } from '@/services/api'
import type {
    BoardGoldCacheScriptsResponse,
    BoardGoldCacheStats,
    BoardGoldCacheUpdateTask,
    BoardGoldExitScanResponse,
    BoardGoldExitSignal,
    BoardGoldResult,
    BoardGoldScanTask,
    BoardGoldSignal,
    BoardGoldStrategiesResponse,
    BoardGoldStrategyInfo,
} from '@/types'

const STRATEGY_LABELS: Record<string, string> = {
    three_yin: '三阴不破阳',
    overnight_hold: '一夜持股',
    shrink_yang: '涨停缩量阳',
    phoenix: '涨停金凤凰',
    triple_volume: '三倍量突破',
    shrink_yin: '涨停缩量阴',
    fixed_exit: '固定止盈止损',
    trailing_exit: '移动止盈',
    phoenix_exit: '金凤凰离场',
}

const DETAIL_LABELS: Record<string, string> = {
    yin_days: '阴线',
    consolidation_days: '整理',
    volume_ratio: '量比',
    base_volume_ratio: '基准量比',
    signal_volume_ratio: '信号量比',
    days_between: '间隔',
    support_price: '支撑',
    buy_date: '买入日',
    buy_price: '买入价',
    limit_up_price: '涨停价',
    first_limit_up_date: '首板',
    second_limit_up_date: '二板',
}

export default function GoldBoard() {
    const navigate = useNavigate()
    const [strategies, setStrategies] = useState<BoardGoldStrategiesResponse | null>(null)
    const [cacheStats, setCacheStats] = useState<BoardGoldCacheStats | null>(null)
    const [cacheScripts, setCacheScripts] = useState<BoardGoldCacheScriptsResponse | null>(null)
    const [cacheTask, setCacheTask] = useState<BoardGoldCacheUpdateTask | null>(null)
    const [latestResult, setLatestResult] = useState<BoardGoldResult | null>(null)
    const [scanTask, setScanTask] = useState<BoardGoldScanTask | null>(null)
    const [exitResult, setExitResult] = useState<BoardGoldExitScanResponse | null>(null)
    const [selectedStrategies, setSelectedStrategies] = useState<string[]>([])
    const [symbolsText, setSymbolsText] = useState('')
    const [days, setDays] = useState(80)
    const [targetDate, setTargetDate] = useState('')
    const [maxStocks, setMaxStocks] = useState('')
    const [exitStrategy, setExitStrategy] = useState('fixed_exit')
    const [loading, setLoading] = useState(true)
    const [starting, setStarting] = useState(false)
    const [cacheStarting, setCacheStarting] = useState(false)
    const [exitLoading, setExitLoading] = useState(false)
    const [error, setError] = useState<string | null>(null)
    const [watchlistMessage, setWatchlistMessage] = useState<string | null>(null)

    const entryStrategies = strategies?.entry_strategies || []
    const exitStrategies = strategies?.exit_strategies || []
    const activeSignals = scanTask?.signals.length ? scanTask.signals : latestResult?.signals || []
    const isScanning = scanTask?.status === 'pending' || scanTask?.status === 'running'
    const isCacheUpdating = cacheTask?.status === 'pending' || cacheTask?.status === 'running'
    const progressPct = scanTask?.total ? Math.round((scanTask.current / scanTask.total) * 100) : 0
    const cacheScriptOptions = cacheScripts?.scripts || []
    const availableCacheRoutes = cacheScriptOptions.filter(item => item.available).length

    const summaryItems = useMemo(() => {
        const summary = latestResult?.summary || {}
        return Object.entries(summary).map(([strategy, value]) => ({
            strategy,
            count: value.count,
        }))
    }, [latestResult])

    const loadInitial = useCallback(async () => {
        setLoading(true)
        setError(null)
        try {
            const [strategyResponse, statsResponse, latestResponse, scriptsResponse] = await Promise.all([
                api.getBoardGoldStrategies(),
                api.getBoardGoldCacheStats(),
                api.getBoardGoldLatestResult(),
                api.getBoardGoldCacheScripts(),
            ])
            const scriptItems = scriptsResponse.scripts || []
            setStrategies(strategyResponse)
            setCacheStats(statsResponse)
            setLatestResult(latestResponse.result)
            setCacheScripts({ ...scriptsResponse, scripts: scriptItems })
            setSelectedStrategies(prev => {
                if (prev.length > 0) return prev
                return strategyResponse.entry_strategies
                    .filter(item => item.enabled)
                    .map(item => item.name)
            })
        } catch (e) {
            setError(e instanceof Error ? e.message : '黄金信号加载失败')
        } finally {
            setLoading(false)
        }
    }, [])

    useEffect(() => {
        void loadInitial()
    }, [loadInitial])

    useEffect(() => {
        if (!scanTask || !isScanning) return
        let cancelled = false
        const intervalId = window.setInterval(async () => {
            try {
                const next = await api.getBoardGoldScanStatus(scanTask.task_id)
                const latest = next.status === 'completed'
                    ? await api.getBoardGoldLatestResult()
                    : null
                if (cancelled) return
                setScanTask(next)
                if (latest) setLatestResult(latest.result)
            } catch (e) {
                if (!cancelled) setError(e instanceof Error ? e.message : '扫描状态刷新失败')
            }
        }, 1400)
        return () => {
            cancelled = true
            window.clearInterval(intervalId)
        }
    }, [isScanning, scanTask])

    useEffect(() => {
        if (!cacheTask || !isCacheUpdating) return
        let cancelled = false
        const intervalId = window.setInterval(async () => {
            try {
                const next = await api.getBoardGoldCacheUpdateStatus(cacheTask.task_id)
                const stats = next.status === 'completed' || next.status === 'failed'
                    ? await api.getBoardGoldCacheStats()
                    : null
                if (cancelled) return
                setCacheTask(next)
                if (stats) setCacheStats(stats)
            } catch (e) {
                if (!cancelled) setError(e instanceof Error ? e.message : '缓存更新状态刷新失败')
            }
        }, 1600)
        return () => {
            cancelled = true
            window.clearInterval(intervalId)
        }
    }, [cacheTask, isCacheUpdating])

    const toggleStrategy = useCallback((strategyName: string) => {
        setSelectedStrategies(prev => (
            prev.includes(strategyName)
                ? prev.filter(item => item !== strategyName)
                : [...prev, strategyName]
        ))
    }, [])

    const parseSymbols = useCallback(() => {
        return symbolsText
            .split(/[\s,，;；]+/)
            .map(item => item.trim())
            .filter(Boolean)
    }, [symbolsText])

    const startCacheUpdate = useCallback(async () => {
        setCacheStarting(true)
        setError(null)
        try {
            const task = await api.startBoardGoldCacheUpdate()
            setCacheTask(task)
        } catch (e) {
            setError(e instanceof Error ? e.message : '启动缓存更新失败')
        } finally {
            setCacheStarting(false)
        }
    }, [])

    const startScan = useCallback(async () => {
        if (selectedStrategies.length === 0) {
            setError('至少选择一个入场策略')
            return
        }
        setStarting(true)
        setError(null)
        setExitResult(null)
        try {
            const task = await api.startBoardGoldScan({
                strategies: selectedStrategies,
                symbols: parseSymbols(),
                days,
                target_date: targetDate || null,
                max_stocks: maxStocks ? Number(maxStocks) : null,
            })
            setScanTask(task)
        } catch (e) {
            setError(e instanceof Error ? e.message : '启动扫描失败')
        } finally {
            setStarting(false)
        }
    }, [days, maxStocks, parseSymbols, selectedStrategies, targetDate])

    const runExitScan = useCallback(async () => {
        if (activeSignals.length === 0) return
        setExitLoading(true)
        setError(null)
        try {
            const result = await api.scanBoardGoldExits(
                activeSignals.map(signal => ({ ...signal })),
                exitStrategy,
                120,
            )
            setExitResult(result)
        } catch (e) {
            setError(e instanceof Error ? e.message : '离场扫描失败')
        } finally {
            setExitLoading(false)
        }
    }, [activeSignals, exitStrategy])

    const addToWatchlist = useCallback(async (symbol: string) => {
        setWatchlistMessage(null)
        try {
            const result = await api.addToWatchlist(symbol)
            setWatchlistMessage(result.message)
        } catch (e) {
            setWatchlistMessage(e instanceof Error ? e.message : '加入自选失败')
        }
    }, [])

    return (
        <div className="space-y-5">
            <div className="flex flex-col gap-4 lg:flex-row lg:items-end lg:justify-between">
                <div>
                    <div className="flex items-center gap-2 text-sm font-medium text-amber-600 dark:text-amber-300">
                        <Gem className="h-4 w-4" />
                        板上有黄金
                    </div>
                    <h1 className="mt-1 text-2xl font-bold text-slate-900 dark:text-slate-100">黄金信号扫描</h1>
                </div>
                <div className="flex flex-wrap items-center gap-2">
                    <button
                        type="button"
                        onClick={() => void loadInitial()}
                        disabled={loading}
                        className="inline-flex items-center gap-1.5 rounded-lg border border-slate-200 bg-white px-3 py-2 text-xs font-medium text-slate-600 transition-colors hover:bg-slate-50 disabled:opacity-50 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-300 dark:hover:bg-slate-800"
                    >
                        {loading ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RefreshCw className="h-3.5 w-3.5" />}
                        刷新
                    </button>
                    <button
                        type="button"
                        onClick={() => void startScan()}
                        disabled={starting || isScanning}
                        className="inline-flex items-center gap-1.5 rounded-lg bg-blue-600 px-3 py-2 text-xs font-medium text-white transition-colors hover:bg-blue-500 disabled:cursor-not-allowed disabled:opacity-50"
                    >
                        {starting || isScanning ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Play className="h-3.5 w-3.5" />}
                        开始扫描
                    </button>
                </div>
            </div>

            {error && (
                <div className="flex items-center gap-2 rounded-lg border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-700 dark:border-rose-500/20 dark:bg-rose-500/10 dark:text-rose-300">
                    <ShieldAlert className="h-4 w-4" />
                    {error}
                </div>
            )}

            <div className="grid grid-cols-1 gap-3 md:grid-cols-4">
                <StatTile
                    icon={Database}
                    label="本地日线"
                    value={cacheStats ? `${cacheStats.daily_count}` : '--'}
                    subValue={cacheStats?.available ? '缓存可用' : '未发现缓存'}
                    tone={cacheStats?.available ? 'emerald' : 'amber'}
                />
                <StatTile
                    icon={Target}
                    label="最新信号"
                    value={latestResult ? `${latestResult.signals_count}` : '--'}
                    subValue={latestResult ? formatDateTime(latestResult.scan_time) : '暂无结果'}
                    tone="blue"
                />
                <StatTile
                    icon={Activity}
                    label="扫描进度"
                    value={scanTask ? `${scanTask.current}/${scanTask.total || 0}` : '--'}
                    subValue={scanTask ? statusLabel(scanTask.status) : '待命'}
                    tone={isScanning ? 'amber' : 'slate'}
                    loading={isScanning}
                />
                <StatTile
                    icon={BarChart3}
                    label="策略覆盖"
                    value={`${selectedStrategies.length}/${entryStrategies.length || 0}`}
                    subValue={summaryItems.length > 0 ? summaryItems.map(item => `${strategyLabel(item.strategy)} ${item.count}`).join(' · ') : '等待扫描'}
                    tone="violet"
                />
            </div>

            <section className="rounded-lg border border-slate-200 bg-white shadow-sm dark:border-slate-700 dark:bg-slate-900">
                <div className="flex flex-col gap-4 border-b border-slate-200 px-4 py-4 dark:border-slate-700 lg:flex-row lg:items-center lg:justify-between">
                    <div className="min-w-0">
                        <div className="flex items-center gap-2">
                            <Database className="h-4 w-4 text-amber-500" />
                            <h2 className="text-sm font-semibold text-slate-900 dark:text-slate-100">本地缓存更新</h2>
                        </div>
                        <div className="mt-1 truncate text-xs text-slate-500 dark:text-slate-400">
                            {cacheStats?.data_dir || 'data/board_gold'}
                        </div>
                    </div>
                    <div className="flex flex-col gap-2 md:flex-row md:items-center">
                        <button
                            type="button"
                            onClick={() => void startCacheUpdate()}
                            disabled={cacheStarting || isCacheUpdating || loading}
                            className="inline-flex items-center justify-center gap-1.5 rounded-lg bg-amber-500 px-3 py-2 text-xs font-medium text-white transition-colors hover:bg-amber-400 disabled:cursor-not-allowed disabled:opacity-50"
                        >
                            {cacheStarting || isCacheUpdating ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RefreshCw className="h-3.5 w-3.5" />}
                            一键更新
                        </button>
                    </div>
                </div>

                <div className="grid grid-cols-1 divide-y divide-slate-200 dark:divide-slate-700 lg:grid-cols-[280px_1fr] lg:divide-x lg:divide-y-0">
                    <div className="px-4 py-4">
                        <div className="grid grid-cols-2 gap-3 text-xs">
                            <div>
                                <div className="text-slate-500 dark:text-slate-400">更新模式</div>
                                <div className="mt-1 font-semibold text-emerald-600 dark:text-emerald-300">
                                    自动
                                </div>
                            </div>
                            <div>
                                <div className="text-slate-500 dark:text-slate-400">任务状态</div>
                                <div className="mt-1 font-semibold text-slate-900 dark:text-slate-100">
                                    {cacheTask ? statusLabel(cacheTask.status) : '待命'}
                                </div>
                            </div>
                            <div>
                                <div className="text-slate-500 dark:text-slate-400">后台通道</div>
                                <div className="mt-1 font-semibold text-slate-900 dark:text-slate-100">
                                    {cacheScriptOptions.length ? `${availableCacheRoutes}/${cacheScriptOptions.length}` : '--'}
                                </div>
                            </div>
                            <div>
                                <div className="text-slate-500 dark:text-slate-400">退出码</div>
                                <div className="mt-1 font-semibold text-slate-900 dark:text-slate-100">
                                    {cacheTask?.exit_code ?? '--'}
                                </div>
                            </div>
                        </div>
                    </div>
                    <div className="px-4 py-4">
                        <div className="mb-2 flex items-center gap-2 text-xs font-medium text-slate-500 dark:text-slate-400">
                            <Terminal className="h-3.5 w-3.5" />
                            输出
                        </div>
                        <pre className="max-h-44 min-h-[72px] overflow-auto whitespace-pre-wrap rounded-lg bg-slate-950 px-3 py-2 text-xs leading-5 text-slate-200">{cacheTask?.logs.length ? cacheTask.logs.slice(-80).join('\n') : '暂无输出'}</pre>
                    </div>
                </div>
            </section>

            <div className="grid grid-cols-1 gap-4 xl:grid-cols-[360px_1fr]">
                <section className="space-y-4 rounded-lg border border-slate-200 bg-white p-4 shadow-sm dark:border-slate-700 dark:bg-slate-900">
                    <div className="flex items-center justify-between">
                        <h2 className="text-sm font-semibold text-slate-900 dark:text-slate-100">扫描配置</h2>
                        <span className="rounded-full bg-slate-100 px-2.5 py-1 text-xs text-slate-500 dark:bg-slate-800 dark:text-slate-400">
                            {cacheStats?.data_dir || 'data/board_gold'}
                        </span>
                    </div>

                    <div className="grid grid-cols-2 gap-3">
                        <label className="space-y-1 text-xs text-slate-500 dark:text-slate-400">
                            K 线窗口
                            <input
                                type="number"
                                min={20}
                                max={260}
                                value={days}
                                onChange={event => setDays(Number(event.target.value))}
                                className="w-full rounded-lg border border-slate-200 bg-white px-3 py-2 text-sm text-slate-800 outline-none focus:border-blue-400 dark:border-slate-700 dark:bg-slate-950 dark:text-slate-100"
                            />
                        </label>
                        <label className="space-y-1 text-xs text-slate-500 dark:text-slate-400">
                            目标日期
                            <input
                                type="date"
                                value={targetDate}
                                onChange={event => setTargetDate(event.target.value)}
                                className="w-full rounded-lg border border-slate-200 bg-white px-3 py-2 text-sm text-slate-800 outline-none focus:border-blue-400 dark:border-slate-700 dark:bg-slate-950 dark:text-slate-100"
                            />
                        </label>
                        <label className="col-span-2 space-y-1 text-xs text-slate-500 dark:text-slate-400">
                            扫描上限
                            <input
                                type="number"
                                min={1}
                                max={6000}
                                value={maxStocks}
                                onChange={event => setMaxStocks(event.target.value)}
                                placeholder="全部"
                                className="w-full rounded-lg border border-slate-200 bg-white px-3 py-2 text-sm text-slate-800 outline-none focus:border-blue-400 dark:border-slate-700 dark:bg-slate-950 dark:text-slate-100"
                            />
                        </label>
                    </div>

                    <label className="block space-y-1 text-xs text-slate-500 dark:text-slate-400">
                        标的
                        <textarea
                            value={symbolsText}
                            onChange={event => setSymbolsText(event.target.value)}
                            placeholder="600519.SH 300750.SZ"
                            className="min-h-[86px] w-full resize-y rounded-lg border border-slate-200 bg-white px-3 py-2 text-sm text-slate-800 outline-none focus:border-blue-400 dark:border-slate-700 dark:bg-slate-950 dark:text-slate-100"
                        />
                    </label>

                    <div className="space-y-2">
                        <div className="text-xs font-medium text-slate-500 dark:text-slate-400">入场策略</div>
                        <div className="grid grid-cols-1 gap-2">
                            {entryStrategies.map(strategy => (
                                <StrategyToggle
                                    key={strategy.name}
                                    strategy={strategy}
                                    checked={selectedStrategies.includes(strategy.name)}
                                    onToggle={() => toggleStrategy(strategy.name)}
                                />
                            ))}
                        </div>
                    </div>
                </section>

                <section className="rounded-lg border border-slate-200 bg-white shadow-sm dark:border-slate-700 dark:bg-slate-900">
                    <div className="border-b border-slate-200 px-4 py-4 dark:border-slate-700">
                        <div className="flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
                            <div>
                                <h2 className="text-sm font-semibold text-slate-900 dark:text-slate-100">入场信号</h2>
                                <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">
                                    {activeSignals.length > 0 ? `${activeSignals.length} 条信号` : '暂无信号'}
                                </p>
                            </div>
                            <div className="flex flex-wrap items-center gap-2">
                                <select
                                    value={exitStrategy}
                                    onChange={event => setExitStrategy(event.target.value)}
                                    className="rounded-lg border border-slate-200 bg-white px-3 py-2 text-xs text-slate-700 outline-none focus:border-blue-400 dark:border-slate-700 dark:bg-slate-950 dark:text-slate-200"
                                >
                                    {exitStrategies.map(strategy => (
                                        <option key={strategy.name} value={strategy.name}>
                                            {strategyLabel(strategy.name)}
                                        </option>
                                    ))}
                                </select>
                                <button
                                    type="button"
                                    onClick={() => void runExitScan()}
                                    disabled={exitLoading || activeSignals.length === 0}
                                    className="inline-flex items-center gap-1.5 rounded-lg border border-slate-200 bg-white px-3 py-2 text-xs font-medium text-slate-600 transition-colors hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-50 dark:border-slate-700 dark:bg-slate-950 dark:text-slate-300 dark:hover:bg-slate-800"
                                >
                                    {exitLoading ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <TrendingUp className="h-3.5 w-3.5" />}
                                    离场扫描
                                </button>
                            </div>
                        </div>

                        {scanTask && (
                            <div className="mt-4">
                                <div className="mb-2 flex items-center justify-between text-xs text-slate-500 dark:text-slate-400">
                                    <span>{statusLabel(scanTask.status)}</span>
                                    <span>{progressPct}%</span>
                                </div>
                                <div className="h-2 overflow-hidden rounded-full bg-slate-100 dark:bg-slate-800">
                                    <div
                                        className="h-full rounded-full bg-blue-500 transition-all"
                                        style={{ width: `${Math.max(0, Math.min(progressPct, 100))}%` }}
                                    />
                                </div>
                            </div>
                        )}
                    </div>

                    <SignalTable
                        signals={activeSignals}
                        onAnalyze={symbol => navigate(`/analysis?symbol=${encodeURIComponent(symbol)}`)}
                        onWatch={symbol => void addToWatchlist(symbol)}
                    />

                    {watchlistMessage && (
                        <div className="border-t border-slate-200 px-4 py-3 text-xs text-slate-500 dark:border-slate-700 dark:text-slate-400">
                            {watchlistMessage}
                        </div>
                    )}
                </section>
            </div>

            {exitResult && (
                <section className="rounded-lg border border-slate-200 bg-white shadow-sm dark:border-slate-700 dark:bg-slate-900">
                    <div className="flex items-center justify-between border-b border-slate-200 px-4 py-4 dark:border-slate-700">
                        <div>
                            <h2 className="text-sm font-semibold text-slate-900 dark:text-slate-100">离场信号</h2>
                            <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">
                                {strategyLabel(exitResult.exit_strategy)} · {exitResult.exit_signals_count} 条
                            </p>
                        </div>
                        <Clock3 className="h-4 w-4 text-slate-400" />
                    </div>
                    <ExitSignalTable signals={exitResult.exit_signals} />
                </section>
            )}
        </div>
    )
}

function StrategyToggle({
    strategy,
    checked,
    onToggle,
}: {
    strategy: BoardGoldStrategyInfo
    checked: boolean
    onToggle: () => void
}) {
    return (
        <button
            type="button"
            onClick={onToggle}
            className={`flex items-start gap-3 rounded-lg border px-3 py-2 text-left transition-colors ${
                checked
                    ? 'border-blue-300 bg-blue-50 text-blue-700 dark:border-blue-500/40 dark:bg-blue-500/10 dark:text-blue-200'
                    : 'border-slate-200 bg-slate-50 text-slate-600 hover:bg-slate-100 dark:border-slate-700 dark:bg-slate-950 dark:text-slate-300 dark:hover:bg-slate-800'
            }`}
        >
            <span className={`mt-0.5 flex h-4 w-4 items-center justify-center rounded border ${
                checked
                    ? 'border-blue-500 bg-blue-500 text-white'
                    : 'border-slate-300 dark:border-slate-600'
            }`}>
                {checked && <CheckCircle2 className="h-3 w-3" />}
            </span>
            <span className="min-w-0">
                <span className="block text-sm font-medium">{strategyLabel(strategy.name)}</span>
                <span className="mt-0.5 block text-xs leading-5 opacity-75">{strategy.description}</span>
            </span>
        </button>
    )
}

function SignalTable({
    signals,
    onAnalyze,
    onWatch,
}: {
    signals: BoardGoldSignal[]
    onAnalyze: (symbol: string) => void
    onWatch: (symbol: string) => void
}) {
    if (signals.length === 0) {
        return (
            <div className="flex min-h-[280px] flex-col items-center justify-center px-4 py-12 text-center text-slate-500 dark:text-slate-400">
                <Gem className="mb-3 h-10 w-10 text-slate-300 dark:text-slate-600" />
                <div className="text-sm font-medium text-slate-600 dark:text-slate-300">没有入场信号</div>
            </div>
        )
    }

    return (
        <div className="overflow-x-auto">
            <table className="min-w-[1040px] divide-y divide-slate-200 text-sm dark:divide-slate-700">
                <thead className="bg-slate-50 text-xs text-slate-500 dark:bg-slate-800/70 dark:text-slate-400">
                    <tr>
                        <Th>标的</Th>
                        <Th>策略</Th>
                        <Th>信号日</Th>
                        <Th>基准日</Th>
                        <Th align="right">价格</Th>
                        <Th align="right">涨跌幅</Th>
                        <Th>结构摘要</Th>
                        <Th align="right">操作</Th>
                    </tr>
                </thead>
                <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
                    {signals.map((signal, index) => (
                        <tr key={`${signal.symbol}-${signal.strategy}-${signal.signal_date}-${index}`} className="hover:bg-slate-50/80 dark:hover:bg-slate-800/40">
                            <Td>
                                <div className="font-semibold text-slate-900 dark:text-slate-100">{signal.name || signal.symbol}</div>
                                <div className="mt-0.5 text-xs text-slate-400">{signal.symbol}</div>
                            </Td>
                            <Td>
                                <span className="rounded-full bg-amber-50 px-2 py-1 text-xs font-medium text-amber-700 dark:bg-amber-500/10 dark:text-amber-300">
                                    {strategyLabel(signal.strategy)}
                                </span>
                            </Td>
                            <Td>{signal.signal_date}</Td>
                            <Td>{signal.base_date}</Td>
                            <Td align="right">{formatPrice(signal.price)}</Td>
                            <Td align="right">
                                <span className={Number(signal.change_pct || 0) >= 0 ? 'text-rose-600 dark:text-rose-300' : 'text-emerald-600 dark:text-emerald-300'}>
                                    {formatPercent(signal.change_pct)}
                                </span>
                            </Td>
                            <Td>
                                <span className="line-clamp-2 text-xs text-slate-500 dark:text-slate-400">
                                    {signalDetails(signal)}
                                </span>
                            </Td>
                            <Td align="right">
                                <div className="flex justify-end gap-2">
                                    <button
                                        type="button"
                                        onClick={() => onAnalyze(signal.symbol)}
                                        className="rounded-lg bg-blue-50 px-2.5 py-1.5 text-xs font-medium text-blue-600 hover:bg-blue-100 dark:bg-blue-500/10 dark:text-blue-300 dark:hover:bg-blue-500/20"
                                    >
                                        分析
                                    </button>
                                    <button
                                        type="button"
                                        onClick={() => onWatch(signal.symbol)}
                                        className="rounded-lg bg-slate-100 px-2.5 py-1.5 text-xs font-medium text-slate-600 hover:bg-slate-200 dark:bg-slate-800 dark:text-slate-300 dark:hover:bg-slate-700"
                                    >
                                        自选
                                    </button>
                                </div>
                            </Td>
                        </tr>
                    ))}
                </tbody>
            </table>
        </div>
    )
}

function ExitSignalTable({ signals }: { signals: BoardGoldExitSignal[] }) {
    if (signals.length === 0) {
        return <div className="px-4 py-8 text-center text-sm text-slate-500 dark:text-slate-400">没有离场信号</div>
    }

    return (
        <div className="overflow-x-auto">
            <table className="min-w-[920px] divide-y divide-slate-200 text-sm dark:divide-slate-700">
                <thead className="bg-slate-50 text-xs text-slate-500 dark:bg-slate-800/70 dark:text-slate-400">
                    <tr>
                        <Th>标的</Th>
                        <Th>入场日</Th>
                        <Th>离场日</Th>
                        <Th>类型</Th>
                        <Th align="right">入场价</Th>
                        <Th align="right">离场价</Th>
                        <Th align="right">收益</Th>
                        <Th align="right">持有</Th>
                    </tr>
                </thead>
                <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
                    {signals.map((signal, index) => (
                        <tr key={`${signal.symbol}-${signal.exit_date}-${index}`}>
                            <Td>
                                <div className="font-semibold text-slate-900 dark:text-slate-100">{signal.name || signal.symbol}</div>
                                <div className="mt-0.5 text-xs text-slate-400">{signal.symbol}</div>
                            </Td>
                            <Td>{signal.entry_date}</Td>
                            <Td>{signal.exit_date}</Td>
                            <Td>{exitTypeLabel(signal.exit_type)}</Td>
                            <Td align="right">{formatPrice(signal.entry_price)}</Td>
                            <Td align="right">{formatPrice(signal.exit_price)}</Td>
                            <Td align="right">
                                <span className={Number(signal.profit_pct || 0) >= 0 ? 'text-rose-600 dark:text-rose-300' : 'text-emerald-600 dark:text-emerald-300'}>
                                    {formatPercent(signal.profit_pct)}
                                </span>
                            </Td>
                            <Td align="right">{signal.hold_days ?? '--'} 天</Td>
                        </tr>
                    ))}
                </tbody>
            </table>
        </div>
    )
}

function StatTile({
    icon: Icon,
    label,
    value,
    subValue,
    tone,
    loading = false,
}: {
    icon: typeof Activity
    label: string
    value: string
    subValue: string
    tone: 'blue' | 'emerald' | 'amber' | 'violet' | 'slate'
    loading?: boolean
}) {
    const toneClass = {
        blue: 'bg-blue-50 text-blue-600 dark:bg-blue-500/10 dark:text-blue-300',
        emerald: 'bg-emerald-50 text-emerald-600 dark:bg-emerald-500/10 dark:text-emerald-300',
        amber: 'bg-amber-50 text-amber-600 dark:bg-amber-500/10 dark:text-amber-300',
        violet: 'bg-violet-50 text-violet-600 dark:bg-violet-500/10 dark:text-violet-300',
        slate: 'bg-slate-100 text-slate-500 dark:bg-slate-800 dark:text-slate-300',
    }[tone]

    return (
        <div className="rounded-lg border border-slate-200 bg-white p-4 shadow-sm dark:border-slate-700 dark:bg-slate-900">
            <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                    <div className="text-xs text-slate-500 dark:text-slate-400">{label}</div>
                    <div className="mt-2 truncate text-2xl font-semibold text-slate-900 dark:text-slate-100">{value}</div>
                    <div className="mt-1 truncate text-xs text-slate-400 dark:text-slate-500">{subValue}</div>
                </div>
                <div className={`flex h-10 w-10 shrink-0 items-center justify-center rounded-lg ${toneClass}`}>
                    {loading ? <Loader2 className="h-4 w-4 animate-spin" /> : <Icon className="h-4 w-4" />}
                </div>
            </div>
        </div>
    )
}

function Th({ children, align = 'left' }: { children: ReactNode; align?: 'left' | 'right' }) {
    return <th className={`px-4 py-3 font-medium ${align === 'right' ? 'text-right' : 'text-left'}`}>{children}</th>
}

function Td({ children, align = 'left' }: { children: ReactNode; align?: 'left' | 'right' }) {
    return <td className={`px-4 py-3 align-middle text-slate-700 dark:text-slate-200 ${align === 'right' ? 'text-right' : 'text-left'}`}>{children}</td>
}

function strategyLabel(name: string) {
    return STRATEGY_LABELS[name] || name
}

function statusLabel(status: string) {
    const labels: Record<string, string> = {
        pending: '等待中',
        running: '扫描中',
        completed: '已完成',
        failed: '失败',
    }
    return labels[status] || status
}

function exitTypeLabel(type: string) {
    const labels: Record<string, string> = {
        profit: '止盈',
        stop_loss: '止损',
        trailing: '移动止盈',
        time: '时间离场',
        phoenix_volume_drop: '放量转弱',
        phoenix_support_break: '跌破支撑',
    }
    return labels[type] || type
}

function signalDetails(signal: BoardGoldSignal) {
    const parts = Object.entries(DETAIL_LABELS)
        .map(([key, label]) => {
            const value = signal[key]
            if (value == null || value === '') return null
            const suffix = key.includes('days') ? '天' : ''
            return `${label} ${String(value)}${suffix}`
        })
        .filter((item): item is string => Boolean(item))
    return parts.length > 0 ? parts.join(' · ') : '结构条件已满足'
}

function formatPrice(value: number | null | undefined) {
    if (typeof value !== 'number' || Number.isNaN(value)) return '--'
    return value.toFixed(2)
}

function formatPercent(value: number | null | undefined) {
    if (typeof value !== 'number' || Number.isNaN(value)) return '--'
    const sign = value > 0 ? '+' : ''
    return `${sign}${value.toFixed(2)}%`
}

function formatDateTime(value?: string | null) {
    if (!value) return '--'
    const date = new Date(value)
    if (Number.isNaN(date.getTime())) return value
    return `${date.getMonth() + 1}/${date.getDate()} ${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}`
}
