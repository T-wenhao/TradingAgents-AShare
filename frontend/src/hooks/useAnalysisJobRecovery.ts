import { useEffect } from 'react'
import { api } from '@/services/api'
import { useAnalysisStore } from '@/stores/analysisStore'
import type { JobStatus, Report } from '@/types'
import { useSSE } from './useSSE'

function isActiveJob(status?: JobStatus['status']) {
    return status === 'pending' || status === 'running'
}

export function useAnalysisJobRecovery() {
    const currentJobId = useAnalysisStore(state => state.currentJobId)
    const isConnected = useAnalysisStore(state => state.isConnected)
    const analysisRunState = useAnalysisStore(state => state.analysisRunState)
    const {
        setCurrentJobId,
        setCurrentSymbol,
        setJobStatus,
        updateAgentSnapshot,
        setReport,
        setStructuredData,
        setIsAnalyzing,
        setAnalysisRunState,
        setCurrentHorizon,
    } = useAnalysisStore()

    const streamJobId = currentJobId && !isConnected && analysisRunState === 'running'
        ? currentJobId
        : null
    useSSE(streamJobId)

    useEffect(() => {
        if (!currentJobId) return

        let cancelled = false

        const applyCompletedResult = async (status: JobStatus) => {
            try {
                const result = await api.getJobResult(status.job_id)
                if (cancelled) return
                setReport(result.result)
            } catch {
                // Status recovery still works even if the completed result has expired.
            }

            if (!status.symbol) return

            try {
                const history = await api.getReports(status.symbol, 0, 10)
                if (cancelled) return
                const matched = history.reports.find((item: Report) => item.id === status.job_id) ?? history.reports[0]
                if (matched) {
                    setStructuredData({
                        riskItems: matched.risk_items,
                        keyMetrics: matched.key_metrics,
                        confidence: matched.confidence,
                        targetPrice: matched.target_price,
                        stopLoss: matched.stop_loss_price,
                    })
                }
            } catch {
                // Structured fields are a nicety; the main status has already been restored.
            }
        }

        const reconcile = async () => {
            try {
                const status = await api.getJobStatus(currentJobId)
                if (cancelled) return

                setJobStatus(status)
                if (status.symbol) setCurrentSymbol(status.symbol)
                if (status.current_horizon !== undefined) setCurrentHorizon(status.current_horizon)
                if (status.agents?.length) {
                    updateAgentSnapshot({ agents: status.agents })
                }

                if (isActiveJob(status.status)) {
                    setIsAnalyzing(true)
                    setAnalysisRunState('running')
                    return
                }

                setIsAnalyzing(false)
                setCurrentHorizon(null)
                if (status.status === 'completed') {
                    setAnalysisRunState('completed')
                    setJobStatus(null)
                    await applyCompletedResult(status)
                } else {
                    setAnalysisRunState('failed', status.error || 'unknown error')
                    setJobStatus(null)
                }
            } catch (error) {
                if (cancelled) return
                console.warn('Failed to recover analysis job:', error)
                setIsAnalyzing(false)
                setAnalysisRunState('idle')
                setCurrentJobId(null)
                setJobStatus(null)
                setCurrentHorizon(null)
            }
        }

        void reconcile()
        const timer = window.setInterval(() => {
            void reconcile()
        }, 5000)

        return () => {
            cancelled = true
            window.clearInterval(timer)
        }
    }, [
        currentJobId,
        setCurrentJobId,
        setCurrentSymbol,
        setJobStatus,
        updateAgentSnapshot,
        setReport,
        setStructuredData,
        setIsAnalyzing,
        setAnalysisRunState,
        setCurrentHorizon,
    ])
}
