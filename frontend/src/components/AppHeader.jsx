import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Layout, Space, Badge, Avatar, Dropdown, Popover, Button, Empty } from 'antd'
import { BellOutlined, UserOutlined, QuestionCircleOutlined } from '@ant-design/icons'

const { Header } = Layout

export default function AppHeader() {
  const [seen, setSeen] = useState(() => { try { return Number(localStorage.getItem('aiac-notifications-seen') || 0) } catch { return 0 } })
  const { data: notices = [], isError } = useQuery({
    queryKey: ['completionNotifications'],
    queryFn: async () => { const r = await fetch('/api/v1/runs/bounded/notifications'); if (!r.ok) throw new Error('Notifications unavailable'); return r.json() },
    refetchInterval: 15000,
  })
  const unread = notices.filter(n => n.id > seen).length
  const markSeen = (open) => {
    if (!open || !notices.length) return
    const latest = Math.max(...notices.map(n => n.id)); setSeen(latest)
    try { localStorage.setItem('aiac-notifications-seen', String(latest)) } catch { /* private browser mode */ }
  }
  const noticeList = <div style={{ maxWidth: 380, maxHeight: 400, overflow: 'auto' }}>
    {isError ? '通知暂不可用' : !notices.length ? <Empty description="暂无完成通知" /> : notices.map(n => <div key={n.id} style={{ padding: '8px 0' }}>
      <a href={`/tasks/${n.task_id}`}>任务 {n.task_id} / Run {n.run_id} 已完成</a>
      <div>结果 {n.facts.requests.RESULT || 0} · PASS {n.facts.quality_PASS} · PPA_READY {n.facts.PPA_READY}</div>
      <div>{n.analysis_status === 'COMPLETED' ? '自动复盘已保存' : '自动复盘失败，需查看'} · {n.analysis.decision}</div>
    </div>)}
  </div>
  const dropdownItems = [
    { key: 'profile', label: '个人中心' },
    { key: 'settings', label: '系统设置' },
    { type: 'divider' },
    { key: 'logout', label: '退出登录' },
  ]

  return (
    <Header style={{
      background: 'rgba(19, 26, 43, 0.8)',
      backdropFilter: 'blur(12px)',
      borderBottom: '1px solid rgba(255, 255, 255, 0.1)',
      display: 'flex',
      alignItems: 'center',
      justifyContent: 'space-between',
      padding: '0 24px',
    }}>
      <div>
        <span style={{
          fontSize: 16,
          fontWeight: 500,
          color: 'rgba(255, 255, 255, 0.85)',
        }}>
          Alpha-GPT 挖掘系统
        </span>
        <span style={{
          marginLeft: 12,
          fontSize: 12,
          color: 'rgba(255, 255, 255, 0.45)',
        }}>
          人机协同 Alpha 工厂
        </span>
      </div>
      
      <Space size={16}>
        <QuestionCircleOutlined style={{ fontSize: 18, color: 'rgba(255, 255, 255, 0.65)' }} />
        
        <Popover title="批次完成通知" content={noticeList} trigger="click" placement="bottomRight" onOpenChange={markSeen}>
          <Badge count={unread} size="small">
            <Button type="text" aria-label="批次完成通知" icon={<BellOutlined />} />
          </Badge>
        </Popover>
        
        <Dropdown menu={{ items: dropdownItems }} placement="bottomRight">
          <Avatar 
            icon={<UserOutlined />} 
            style={{ 
              backgroundColor: '#00d4ff',
              cursor: 'pointer',
            }} 
          />
        </Dropdown>
      </Space>
    </Header>
  )
}
