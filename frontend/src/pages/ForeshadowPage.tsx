import { useCallback, useEffect, useRef, useState } from "react"
import { useParams } from "react-router-dom"
import {
  fetchForeshadowAnalysis,
  fetchForeshadowStatus,
  startForeshadowAnalysis,
  type ForeshadowItem,
} from "@/api/client"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { trackEvent } from "@/lib/tracker"

export default function ForeshadowPage() {
  const { novelId } = useParams<{ novelId: string }>()

  const [items, setItems] = useState<ForeshadowItem[]>([])
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
        fetchForeshadowAnalysis(novelId),
        fetchForeshadowStatus(novelId),
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
    trackEvent("view_foreshadow")
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
      await startForeshadowAnalysis(novelId)
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
            <span>伏笔回收 · 20章窗口</span>
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
          以 20 章为窗口识别<b>伏笔的埋设与回收</b>：谁在何时埋下线索（身世疑点 / 物件去向 / 预言 /
          未解之局），又在何时回收呼应；标注<b>已回收</b>与<b>待回收</b>，帮助检查线索闭环。
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
          还没有伏笔回收拆解。点击上方「开始拆解全书」生成窗口伏笔清单。
        </div>
      ) : (
        <div className="space-y-4">
          {items.map((item) => (
            <ForeshadowWindowCard key={item.arc_start} item={item} />
          ))}
        </div>
      )}
    </div>
  )
}

function ForeshadowWindowCard({ item }: { item: ForeshadowItem }) {
  const { foreshadow } = item
  const recalled = foreshadow.foreshadows.filter((f) => f.status === "已回收").length
  const pending = foreshadow.foreshadows.filter((f) => f.status === "待回收").length
  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="flex flex-wrap items-baseline gap-3 text-base">
          <span className="shrink-0 text-muted-foreground tabular-nums">
            第{item.arc_start}–{item.arc_end}章
          </span>
          <span className="flex-1 text-sm font-normal leading-relaxed text-foreground">
            {foreshadow.window_summary}
          </span>
          <span className="flex shrink-0 items-center gap-1.5 text-xs">
            <span className="rounded bg-emerald-500/15 px-1.5 py-0.5 text-emerald-600 dark:text-emerald-400">
              已回收 {recalled}
            </span>
            <span className="rounded bg-amber-500/15 px-1.5 py-0.5 text-amber-600 dark:text-amber-400">
              待回收 {pending}
            </span>
          </span>
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-2 pt-1">
        {foreshadow.foreshadows.map((f, i) => (
          <div
            key={i}
            className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded-md bg-muted/50 p-3"
          >
            <span className="text-xs text-muted-foreground">
              埋设
              <b className="mx-1 text-foreground tabular-nums">
                {f.plant_chapter === 0 ? "前文" : `第${f.plant_chapter}章`}
              </b>
            </span>
            <span className="text-muted-foreground">→</span>
            <span className="text-xs text-muted-foreground">
              回收
              <b className="mx-1 text-foreground tabular-nums">
                {f.recall_chapter === null ? "未回收" : `第${f.recall_chapter}章`}
              </b>
            </span>
            <span
              className={`rounded px-1.5 py-0.5 text-xs font-medium ${
                f.status === "已回收"
                  ? "bg-emerald-500/15 text-emerald-600 dark:text-emerald-400"
                  : "bg-amber-500/15 text-amber-600 dark:text-amber-400"
              }`}
            >
              {f.status}
            </span>
            <div className="w-full text-sm leading-relaxed text-foreground/90">
              {f.content}
              {f.note && (
                <span className="text-muted-foreground"> —— {f.note}</span>
              )}
            </div>
          </div>
        ))}
      </CardContent>
    </Card>
  )
}
