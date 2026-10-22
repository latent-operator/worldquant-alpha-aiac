const waitingLabels = {
  DAILY_QUOTA: '等待 LLM 额度',
  MALFORMED_HYPOTHESIS: '等待假设生成恢复',
  MALFORMED_RESPONSE: '等待生成输出恢复',
  NO_NEW_CANDIDATES: '等待新候选生成',
  DEDUP_UNAVAILABLE: '等待去重服务恢复',
  WQ_SIMULATION_ERROR: '等待 WQ 仿真恢复',
  INVALID_CREDENTIALS: '等待凭证更新',
}

export const waitingStatusText = (reason) => waitingLabels[reason] || '等待自动恢复'

export const waitingHealthText = (reason) => {
  if (reason === 'DAILY_QUOTA') return '额度待恢复'
  if (reason === 'DEDUP_UNAVAILABLE') return '去重服务待恢复'
  if (reason === 'WQ_SIMULATION_ERROR') return 'WQ 仿真待恢复'
  if (reason === 'INVALID_CREDENTIALS') return '凭证待更新'
  return '生成待恢复'
}
