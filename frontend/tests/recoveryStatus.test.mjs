import test from 'node:test'
import assert from 'node:assert/strict'
import { waitingStatusText, waitingHealthText } from '../src/utils/recoveryStatus.js'

test('quota waiting is distinct from generation recovery', () => {
  assert.equal(waitingStatusText('DAILY_QUOTA'), '等待 LLM 额度')
  assert.equal(waitingStatusText('NO_NEW_CANDIDATES'), '等待新候选生成')
  assert.equal(waitingStatusText('MALFORMED_RESPONSE'), '等待生成输出恢复')
  assert.equal(waitingStatusText('DEDUP_UNAVAILABLE'), '等待去重服务恢复')
  assert.equal(waitingHealthText('NO_NEW_CANDIDATES'), '生成待恢复')
  assert.equal(waitingHealthText('DEDUP_UNAVAILABLE'), '去重服务待恢复')
  assert.equal(waitingHealthText('WQ_SIMULATION_ERROR'), 'WQ 仿真待恢复')
})
