import { useCallback, useEffect, useRef, useState } from "react"
import { useParams } from "react-router-dom"
import {
  fetchBorrowableAnalysis,
  fetchBorrowableStatus,
  startBorrowableAnalysis,
  type BorrowItem,
} from "@/api/client"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { trackEvent } from "@/lib/tracker"

export default function BorrowablePage() {
  const { novelId } = useParams<{ novelId: string }>()

  const [items, setItems] = useState<BorrowItem[]>([])
  const [status, setStatus] = useState<{
    total_chapters: number
    total_windows: number
    analyzed_windows: number
    running: boolean
  } | null>(null)
  const [loading, setLoading] = useState(true)
  const [starting, setStarting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)

  const load = useCallback(async () => {
    if (!novelId) return
    try {
      const [m, s] = await Promise.all([
        fetchBorrowableAnalysis(novelId),
        fetchBorrowableStatus(novelId),
      ])
      setItems(m.items)
      setStatus(s)
    } catch (err) {
      setError(err instanceof Error ? err.message : "加载失败")
    } finally {
      setLoading(false)
    }
  }, [novelId])

  useEffect(() => {
    if (!novelId) return
    setLoading(true)
    setError(null)
    trackEvent("view_borrowable")
    void load()
    return () => {
      if (pollRef.current) clearInterval(pollRef.current)
    }
  }, [novelId, load])

  // 任务运行中时轮询进度
  useEffect(() => {
    if (!status?.running) {
      if (pollRef.current) {
        clearInterval(pollRef.current)
        pollRef.current = null
      }
      return
    }
    if (pollRef.current) return
    pollRef.current = setInterval(() => {
      void load()
    }, 5000)
    return () => {
      if (pollRef.current) {
        clearInterval(pollRef.current)
        pollRef.current = null
      }
    }
  }, [status?.running, load])

  const handleStart = async () => {
    if (!novelId) return
    setStarting(true)
    setError(null)
    try {
      await startBorrowableAnalysis(novelId)
    } catch (err) {
      setError(err instanceof Error ? err.message : "启动失败")
    } finally {
      setStarting(false)
      void load()
    }
  }

  const analyzed = status?.analyzed_windows ?? 0
  const total = status?.total_windows ?? 0
  const running = status?.running ?? false

  return (
    <div className="mx-auto h-full max-w-5xl space-y-4 overflow-y-auto p-4 md:p-6">
      <Card>
        <CardHeader>
          <CardTitle className="flex flex-wrap items-center justify-between gap-3">
            <span>可借鉴性设定 · 20章窗口</span>
            <div className="flex items-center gap-2 text-sm font-normal">
              <span className="text-muted-foreground tabular-nums">
                {analyzed}/{total} 窗口
              </span>
              {running && (
                <span className="inline-flex items-center gap-1 text-amber-600">
                  <span className="h-2 w-2 animate-pulse rounded-full bg-amber-500" />
                  拆解中
                </span>
              )}
              <Button
                size="sm"
                onClick={() => void handleStart()}
                disabled={starting || running || analyzed >= total}
              >
                {analyzed >= total && !running
                  ? "已全部拆解"
                  : running
                    ? "拆解中…"
                    : analyzed > 0
                      ? "补拆剩余窗口"
                      : "开始拆解全书"}
              </Button>
            </div>
          </CardTitle>
        </CardHeader>
        <CardContent className="text-sm text-muted-foreground">
          以 20 章为窗口提炼可借鉴性设定：<b>爽点</b>（升级 / 金手指 / 抽奖 / 打脸等触发方式）、
          <b>地图情节</b>（借地图 / 藏宝图 / 地点转移推进剧情）、<b>特殊设定</b>（本书独有的世界观机制）。
        </CardContent>
      </Card>

      {error && (
        <div className="rounded-md border border-red-200 bg-red-50 p-3 text-sm text-red-700 dark:border-red-900 dark:bg-red-950/30 dark:text-red-400">
          {error}
        </div>
      )}

      {loading && items.length === 0 ? (
        <div className="py-10 text-center text-sm text-muted-foreground">加载中…</div>
      ) : items.length === 0 ? (
        <div className="rounded-md border border-dashed p-10 text-center text-sm text-muted-foreground">
          还没有可借鉴性设定拆解。点击上方「开始拆解全书」生成窗口设定。
        </div>
      ) : (
        <div className="space-y-4">
          {items.map((item) => (
            <BorrowWindowCard key={item.arc_start} item={item} />
          ))}
        </div>
      )}
    </div>
  )
}

const COOL_TYPE_COLORS: Record<string, string> = {
  升级突破: "bg-emerald-500/15 text-emerald-600 dark:text-emerald-400",
  金手指: "bg-violet-500/15 text-violet-600 dark:text-violet-400",
  抽奖: "bg-amber-500/15 text-amber-600 dark:text-amber-400",
  打脸: "bg-red-500/15 text-red-600 dark:text-red-400",
  收获: "bg-sky-500/15 text-sky-600 dark:text-sky-400",
  反转: "bg-fuchsia-500/15 text-fuchsia-600 dark:text-fuchsia-400",
  装逼: "bg-orange-500/15 text-orange-600 dark:text-orange-400",
  打斗: "bg-rose-500/15 text-rose-600 dark:text-rose-400",
}

function BorrowWindowCard({ item }: { item: BorrowItem }) {
  const { borrow } = item
  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="flex flex-wrap items-baseline gap-3 text-base">
          <span className="shrink-0 text-muted-foreground tabular-nums">
            第{item.arc_start}–{item.arc_end}章
          </span>
          <span className="flex-1 text-sm font-normal leading-relaxed text-foreground">
            {borrow.window_summary}
          </span>
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-4 pt-1">
        <section>
          <h4 className="mb-2 text-sm font-semibold text-foreground">
            爽点 · 值得学习的爽感设计
          </h4>
          <ul className="space-y-2">
            {borrow.cool_points.map((c, i) => (
              <li key={i} className="rounded-md bg-muted/50 p-3">
                <div className="mb-1 flex flex-wrap items-center gap-2">
                  <span className="text-xs text-muted-foreground tabular-nums">
                    第{c.chapter}章
                  </span>
                  <span
                    className={`rounded px-1.5 py-0.5 text-xs font-medium ${
                      COOL_TYPE_COLORS[c.type] ?? "bg-muted text-muted-foreground"
                    }`}
                  >
                    {c.type}
                  </span>
                  <span className="font-medium">{c.title}</span>
                </div>
                <div className="text-sm leading-relaxed text-foreground/90">
                  {c.description}
                </div>
              </li>
            ))}
          </ul>
        </section>

        <section>
          <h4 className="mb-2 text-sm font-semibold text-foreground">
            地图情节 · 借地图推进剧情
          </h4>
          {borrow.map_plots.length === 0 ? (
            <p className="text-sm text-muted-foreground">本窗口没有地图情节。</p>
          ) : (
            <ul className="space-y-2">
              {borrow.map_plots.map((m, i) => (
                <li key={i} className="flex gap-2 text-sm leading-relaxed">
                  <span className="shrink-0 text-xs text-muted-foreground tabular-nums">
                    第{m.chapter}章
                  </span>
                  <span className="text-foreground/90">
                    <b className="font-medium">{m.title}</b>
                    <span className="text-muted-foreground"> —— {m.description}</span>
                  </span>
                </li>
              ))}
            </ul>
          )}
        </section>

        <section>
          <h4 className="mb-2 text-sm font-semibold text-foreground">
            特殊设定 · 本书独有的世界观机制
          </h4>
          <ul className="space-y-2">
            {borrow.special_settings.map((s, i) => (
              <li key={i} className="flex gap-2 text-sm leading-relaxed">
                <span className="shrink-0 text-xs text-muted-foreground tabular-nums">
                  第{s.chapter}章
                </span>
                <span className="text-foreground/90">
                  <b className="font-medium">{s.title}</b>
                  <span className="text-muted-foreground"> —— {s.description}</span>
                </span>
              </li>
            ))}
          </ul>
        </section>
      </CardContent>
    </Card>
  )
}
