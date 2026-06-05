import { useEffect, useRef, useCallback, useState } from 'react'
import { useAnalysisStore } from '@/stores/analysisStore'
import type { AnalysisReport, RiskItem, KeyMetric } from '@/types'
import { getAuthToken, getBaseUrl } from '@/services/api'

export function useSSE(jobId: string | null) {
    const controllerRef = useRef<AbortController | null>(null)
    const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
    const agentMessageMapRef = useRef<Record<string, string>>({})
    const firstTokenMapRef = useRef<Record<string, boolean>>({})
    const [retryTick, setRetryTick] = useState(0)

    const {
        setCurrentJobId,
        setCurrentSymbol,
        setJobStatus,
        setIsConnected,
        updateAgentStatus,
        updateAgentSnapshot,
        addAgentMessage,
        addAgentToolCall,
        addAgentReport,
        addReportChunk,
        addMilestone,
        addLog,
        setReport,
        setStructuredData,
        setIsAnalyzing,
        setAnalysisRunState,
        setCurrentHorizon,
        addChatMessage,
        appendToChatMessage,
        setMessageContent,
        markAgentMessagesComplete,
        addDebateMessage,
        appendDebateToken,
    } = useAnalysisStore()

    const connect = useCallback(() => {
        if (!jobId || controllerRef.current) return

        const controller = new AbortController()
        controllerRef.current = controller
        const url = `${getBaseUrl()}/v1/jobs/${jobId}/events`

        const parseData = (raw: string): Record<string, unknown> | null => {
            if (!raw || raw === '[DONE]') return null
            try {
                return JSON.parse(raw) as Record<string, unknown>
            } catch (error) {
                console.error('Failed to parse SSE payload:', error, raw)
                return null
            }
        }

        const handleAgentInProgress = (agentName: string, horizon?: string) => {
            const agentKey = `${agentName}-${horizon || 'main'}`
            if (agentMessageMapRef.current[agentKey]) return

            const horizonLabel = horizon ? `(${horizon === 'short' ? '短线' : '中线'})` : ''
            const msgId = `agent-msg-${agentName}-${horizon || 'main'}-${Date.now()}`
            agentMessageMapRef.current[agentKey] = msgId
            firstTokenMapRef.current[msgId] = true

            addChatMessage({
                id: msgId,
                role: 'assistant',
                agent: agentName,
                content: `**${agentName}** ${horizonLabel} 正在思考并撰写报告中...`,
                timestamp: new Date().toISOString(),
            })
        }

        const handleEvent = (eventType: string, data: Record<string, unknown>) => {
            switch (eventType) {
                case 'job.ready':
                    if (typeof data.job_id === 'string' && data.job_id) {
                        setCurrentJobId(data.job_id)
                    }
                    break

                case 'job.created': {
                    const createdJobId = String(data.job_id || '')
                    const symbol = String(data.symbol || '')
                    if (createdJobId) setCurrentJobId(createdJobId)
                    if (symbol) setCurrentSymbol(symbol)
                    addLog({
                        id: Date.now().toString(),
                        timestamp: new Date().toISOString(),
                        type: 'system',
                        content: `Job created: ${createdJobId}`,
                    })
                    break
                }

                case 'job.running': {
                    const symbol = String(data.symbol || '')
                    setIsAnalyzing(true)
                    setAnalysisRunState('running')
                    if (symbol) setCurrentSymbol(symbol)
                    addLog({
                        id: Date.now().toString(),
                        timestamp: new Date().toISOString(),
                        type: 'system',
                        content: `Analysis resumed for ${symbol}`,
                    })
                    break
                }

                case 'job.completed':
                    setCurrentHorizon(null)
                    setIsAnalyzing(false)
                    setAnalysisRunState('completed')
                    setJobStatus(null)
                    markAgentMessagesComplete()
                    setReport((data.result || null) as AnalysisReport | null)
                    setStructuredData({
                        riskItems: (data.risk_items as RiskItem[] | undefined) ?? [],
                        keyMetrics: (data.key_metrics as KeyMetric[] | undefined) ?? [],
                        confidence: data.confidence as number | null | undefined,
                        targetPrice: data.target_price as number | null | undefined,
                        stopLoss: data.stop_loss_price as number | null | undefined,
                    })
                    addChatMessage({
                        id: `job-complete-${Date.now()}`,
                        role: 'system',
                        content: `分析完成。最终建议：${String(data.decision || '')}`,
                        timestamp: new Date().toISOString(),
                    })
                    break

                case 'job.failed':
                    setCurrentHorizon(null)
                    setIsAnalyzing(false)
                    setAnalysisRunState('failed', String(data.error || 'unknown error'))
                    setJobStatus(null)
                    addChatMessage({
                        id: `job-failed-${Date.now()}`,
                        role: 'system',
                        content: `分析失败: ${String(data.error || '未知错误')}`,
                        timestamp: new Date().toISOString(),
                    })
                    break

                case 'agent.horizon_start': {
                    const horizon = typeof data.horizon === 'string' ? data.horizon : ''
                    setCurrentHorizon(horizon || null)
                    break
                }

                case 'agent.horizon_done':
                    break

                case 'agent.status': {
                    const statusData = data as { agent: string; status: string; horizon?: string }
                    if (statusData.status === 'in_progress') {
                        handleAgentInProgress(statusData.agent, statusData.horizon)
                    } else if (statusData.status === 'completed' || statusData.status === 'skipped') {
                        const msgId = agentMessageMapRef.current[`${statusData.agent}-${statusData.horizon || 'main'}`]
                        if (msgId) markAgentMessagesComplete([msgId])
                    }
                    updateAgentStatus(data as {
                        agent: string
                        status: 'pending' | 'in_progress' | 'completed' | 'error' | 'skipped'
                        previous_status?: 'pending' | 'in_progress' | 'completed' | 'error' | 'skipped'
                    })
                    break
                }

                case 'agent.snapshot':
                    updateAgentSnapshot(data as {
                        agents: Array<{
                            team: string
                            agent: string
                            status: 'pending' | 'in_progress' | 'completed' | 'error' | 'skipped'
                        }>
                    })
                    break

                case 'agent.token': {
                    const tokenData = data as { agent: string; report: string; token: string; horizon?: string }
                    if (tokenData.agent === '意图解析') break

                    const agentKey = `${tokenData.agent}-${tokenData.horizon || 'main'}`
                    if (!agentMessageMapRef.current[agentKey]) {
                        handleAgentInProgress(tokenData.agent, tokenData.horizon)
                    }
                    const targetMsgId = agentMessageMapRef.current[agentKey]

                    if (targetMsgId) {
                        if (firstTokenMapRef.current[targetMsgId]) {
                            const horizonText = tokenData.horizon ? `(${tokenData.horizon === 'short' ? '短线' : '中线'})` : ''
                            setMessageContent(targetMsgId, `### ${tokenData.agent} ${horizonText}\n\n${tokenData.token}`)
                            firstTokenMapRef.current[targetMsgId] = false
                        } else {
                            appendToChatMessage(targetMsgId, tokenData.token)
                        }
                    }
                    break
                }

                case 'agent.message':
                    addAgentMessage(data as { agent: string | null; message_type: string | null; content: string })
                    break

                case 'agent.tool_call':
                    addAgentToolCall(data as { agent: string | null; tool_call: { name: string; args: Record<string, unknown> } })
                    break

                case 'agent.report':
                    addAgentReport(data as { section: string; content: string })
                    break

                case 'agent.report.chunk':
                    addReportChunk(data as { section: string; chunk: string; index: number; is_complete: boolean })
                    break

                case 'agent.milestone':
                    addMilestone(data as { stage: string; title: string; summary: string; timestamp: string })
                    break

                case 'agent.debate.token': {
                    const raw = data as Record<string, unknown>
                    const debate = raw.debate
                    if (
                        (debate !== 'research' && debate !== 'risk') ||
                        typeof raw.agent !== 'string' ||
                        typeof raw.round !== 'number' ||
                        typeof raw.token !== 'string'
                    ) break
                    appendDebateToken(
                        debate,
                        raw.agent,
                        raw.round,
                        raw.token,
                        typeof raw.horizon === 'string' ? raw.horizon : undefined,
                    )
                    break
                }

                case 'agent.debate': {
                    const raw = data as Record<string, unknown>
                    const debate = raw.debate
                    if (
                        (debate !== 'research' && debate !== 'risk') ||
                        typeof raw.agent !== 'string' ||
                        typeof raw.round !== 'number' ||
                        typeof raw.content !== 'string'
                    ) break
                    addDebateMessage({
                        debate,
                        agent: raw.agent,
                        round: raw.round,
                        content: raw.content,
                        isVerdict: raw.is_verdict === true,
                        horizon: typeof raw.horizon === 'string' ? raw.horizon : undefined,
                    })
                    break
                }
            }
        }

        const dispatchBlock = (block: string) => {
            const lines = block.split('\n')
            let eventType = 'message'
            const dataLines: string[] = []

            for (const raw of lines) {
                const line = raw.trim()
                if (!line || line.startsWith(':')) continue
                if (line.startsWith('event:')) {
                    eventType = line.slice(6).trim()
                } else if (line.startsWith('data:')) {
                    dataLines.push(line.slice(5).trim())
                }
            }

            const rawData = dataLines.join('\n')
            if (!rawData) return false
            if (eventType === 'done' || rawData === '[DONE]') return true
            if (eventType === 'ping') return false

            const payload = parseData(rawData)
            if (payload) handleEvent(eventType, payload)
            return false
        }

        const run = async () => {
            let streamFinished = false
            try {
                const token = getAuthToken()
                const response = await fetch(url, {
                    headers: {
                        ...(token ? { Authorization: `Bearer ${token}` } : {}),
                    },
                    signal: controller.signal,
                })

                if (!response.ok) {
                    throw new Error(`SSE reconnect failed: ${response.status}`)
                }
                if (!response.body) {
                    throw new Error('SSE stream unavailable')
                }

                setIsConnected(true)
                const reader = response.body.getReader()
                const decoder = new TextDecoder()
                let buffer = ''

                while (true) {
                    const { value, done } = await reader.read()
                    if (done) break

                    buffer += decoder.decode(value, { stream: true })
                    const blocks = buffer.split('\n\n')
                    buffer = blocks.pop() || ''

                    for (const block of blocks) {
                        const finished = dispatchBlock(block)
                        if (finished) {
                            streamFinished = true
                            controller.abort()
                            break
                        }
                    }
                }

                if (!streamFinished && !controller.signal.aborted) {
                    reconnectTimerRef.current = setTimeout(() => {
                        controllerRef.current = null
                        setRetryTick(tick => tick + 1)
                    }, 3000)
                }
            } catch (error) {
                if (!controller.signal.aborted) {
                    console.error('SSE error:', error)
                    setIsConnected(false)
                    addLog({
                        id: Date.now().toString(),
                        timestamp: new Date().toISOString(),
                        type: 'error',
                        content: error instanceof Error ? error.message : 'Connection error',
                    })

                    reconnectTimerRef.current = setTimeout(() => {
                        controllerRef.current = null
                        setRetryTick(tick => tick + 1)
                    }, 3000)
                }
            } finally {
                if (controllerRef.current === controller) {
                    controllerRef.current = null
                }
                setIsConnected(false)
            }
        }

        void run()
    }, [
        jobId,
        setCurrentJobId,
        setCurrentSymbol,
        setJobStatus,
        setIsConnected,
        updateAgentStatus,
        updateAgentSnapshot,
        addAgentMessage,
        addAgentToolCall,
        addAgentReport,
        addReportChunk,
        addMilestone,
        addLog,
        setReport,
        setStructuredData,
        setIsAnalyzing,
        setAnalysisRunState,
        setCurrentHorizon,
        addChatMessage,
        appendToChatMessage,
        setMessageContent,
        markAgentMessagesComplete,
        addDebateMessage,
        appendDebateToken,
    ])

    useEffect(() => {
        connect()
        return () => {
            if (reconnectTimerRef.current) {
                clearTimeout(reconnectTimerRef.current)
                reconnectTimerRef.current = null
            }
            if (controllerRef.current) {
                controllerRef.current.abort()
                controllerRef.current = null
            }
            setIsConnected(false)
        }
    }, [connect, retryTick, setIsConnected])

    const disconnect = useCallback(() => {
        if (reconnectTimerRef.current) {
            clearTimeout(reconnectTimerRef.current)
            reconnectTimerRef.current = null
        }
        if (controllerRef.current) {
            controllerRef.current.abort()
            controllerRef.current = null
            setIsConnected(false)
        }
    }, [setIsConnected])

    return { disconnect }
}
