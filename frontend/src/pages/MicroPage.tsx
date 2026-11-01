import { useCallback, useEffect, useRef, useState } from "react"
import { useParams } from "react-router-dom"
import {
  fetchMicroAnalysis,
  fetchMicroStatus,
  startMicroAnalysis,
  type MicroItem,
} from "@/api/client"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { cn } from "@/lib/utils"
import { trackEvent } from "@/lib/tracker"

export default function MicroPage() {
  const { novelId } = useParams<{ novelId: string }>()

  const [items, setItems] = useState<MicroItem[]>([])
  const [status, setStatus] = useState<{
    total_chapters: number
    analyzed_chapters: number
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
        fetchMicroAnalysis(novelId),
        fetchMicroStatus(novelId),
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
    trackEvent("view_micro_analysis")
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
    }, 4000)
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
      await startMicroAnalysis(novelId)
    } catch (err) {
      setError(err instanceof Error ? err.message : "启动失败")
    } finally {
      setStarting(false)
      void load()
    }
  }

  const analyzed = status?.analyzed_chapters ?? 0
  const total = status?.total_chapters ?? 0
  const running = status?.running ?? false

  return (
    <div className="mx-auto max-w-4xl space-y-4 p-4 md:p-6">
      <Card>
        <CardHeader>
          <CardTitle className="flex flex-wrap items-center justify-between gap-3">
            <span>细章拆解 · 每章结构小结</span>
            <div className="flex items-center gap-2 text-sm font-normal">
              <span className="text-muted-foreground tabular-nums">
                {analyzed}/{total} 章
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
                      ? "补拆剩余章节"
                      : "开始拆解全书"}
              </Button>
            </div>
          </CardTitle>
        </CardHeader>
        <CardContent className="text-sm text-muted-foreground">
          每章输出不超过 100 字的总览 + 3-6 条结构要点（冲突引入 / 升级 / 反转 /
          伏笔 / 情绪渲染 / 收束钩子），供拆解与借鉴。
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
          还没有细章拆解。点击上方「开始拆解全书」为这本小说生成每章结构小结。
        </div>
      ) : (
        <div className="space-y-3">
          {items.map((item) => (
            <ChapterMicroCard key={item.chapter_num} item={item} />
          ))}
        </div>
      )}
    </div>
  )
}

function ChapterMicroCard({ item }: { item: MicroItem }) {
  const [expanded, setExpanded] = useState(false)
  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle
          className={cn(
            "flex items-start justify-between gap-3 text-base",
            "cursor-pointer select-none",
          )}
          onClick={() => setExpanded((v) => !v)}
        >
          <span className="shrink-0 text-muted-foreground tabular-nums">
            第{item.chapter_num}章
          </span>
          <span className="flex-1 text-right text-sm font-normal leading-relaxed text-foreground">
            {item.micro.summary}
          </span>
          <span className="shrink-0 text-xs text-muted-foreground">
            {expanded ? "收起" : "展开"}
          </span>
        </CardTitle>
      </CardHeader>
      {expanded && (
        <CardContent className="pt-1">
          <ul className="space-y-1.5">
            {item.micro.points.map((p, i) => (
              <li key={i} className="flex gap-2 text-sm leading-relaxed">
                <span className="mt-0.5 shrink-0 text-xs text-muted-foreground tabular-nums">
                  {i + 1}.
                </span>
                <span className="text-foreground/90">{p}</span>
              </li>
            ))}
          </ul>
        </CardContent>
      )}
    </Card>
  )
}
