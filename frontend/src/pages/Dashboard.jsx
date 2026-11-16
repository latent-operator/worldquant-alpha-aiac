import { useState, useEffect } from 'react'
import { useQuery } from '@tanstack/react-query'
import { 
  Row, 
  Col, 
  Card, 
  Statistic, 
  Progress, 
  Typography, 
  Tag, 
  List,
  Space,
  Spin,
} from 'antd'
import {
  RocketOutlined,
  CheckCircleOutlined,
  ClockCircleOutlined,
  LineChartOutlined,
  ThunderboltOutlined,
} from '@ant-design/icons'
import { 
  LineChart, 
  Line, 
  XAxis, 
  YAxis, 
  CartesianGrid, 
  Tooltip, 
  ResponsiveContainer 
} from 'recharts'
import api from '../services/api'

const { Title, Text } = Typography

// Placeholder chart data
const mockPnLData = [
  { date: '01-01', returns: 0 },
  { date: '01-05', returns: 2.1 },
  { date: '01-10', returns: 3.5 },
  { date: '01-15', returns: 2.8 },
  { date: '01-20', returns: 5.2 },
  { date: '01-24', returns: 7.8 },
]

export default function Dashboard() {
  const [liveFeed, setLiveFeed] = useState([])

  // Fetch daily stats
  const { data: dailyStats, isLoading: statsLoading } = useQuery({
    queryKey: ['dailyStats'],
    queryFn: () => api.getDailyStats(),
    refetchInterval: 30000, // Refresh every 30 seconds
  })

  // Fetch KPI metrics
  const { data: kpi, isLoading: kpiLoading } = useQuery({
    queryKey: ['kpiMetrics'],
    queryFn: () => api.getKPIMetrics(),
    refetchInterval: 30000,
  })

  // Fetch active tasks
  const { data: activeTasks, isLoading: tasksLoading } = useQuery({
    queryKey: ['activeTasks'],
    queryFn: () => api.getActiveTasks(),
    refetchInterval: 5000,
  })

  // Live feed SSE connection
  useEffect(() => {
    const eventSource = new EventSource('/api/v1/stats/live-feed')
    
    eventSource.onmessage = (event) => {
      const data = JSON.parse(event.data)
      setLiveFeed(prev => [data, ...prev.slice(0, 49)]) // Keep last 50
    }

    eventSource.onerror = () => {
      console.log('SSE connection error, will retry...')
    }

    return () => eventSource.close()
  }, [])

  const stats = dailyStats || { goal: 4, current: 0, success_rate: 0, avg_sharpe: 0 }
  const metrics = kpi || { today_simulations: 0, today_success_rate: 0, today_avg_sharpe: 0, week_total_alphas: 0 }
  const tasks = activeTasks || []

  const goalPercent = Math.round((stats.current / stats.goal) * 100)

  return (
    <div>
      <Title level={3} style={{ marginBottom: 24 }}>
        <RocketOutlined style={{ marginRight: 12, color: '#00d4ff' }} />
        仪表盘
      </Title>

      {/* Top Row: Goal + Active Task + System Health */}
      <Row gutter={[16, 16]}>
        {/* Daily Goal Card */}
        <Col xs={24} sm={12} lg={8}>
          <Card className="glass-card">
            <div style={{ textAlign: 'center' }}>
              <Text type="secondary">今日挖掘目标</Text>
              <div style={{ margin: '16px 0' }}>
                <Progress
                  type="circle"
                  percent={goalPercent}
                  format={() => `${stats.current}/${stats.goal}`}
                  strokeColor="#00d4ff"
                  trailColor="rgba(255,255,255,0.1)"
                  size={120}
                />
              </div>
              <Text style={{ color: '#00ff88' }}>
                {stats.current} 个今日新 Alpha
              </Text>
            </div>
          </Card>
        </Col>

        {/* Active Task Card */}
        <Col xs={24} sm={12} lg={8}>
          <Card className="glass-card">
            <Text type="secondary">当前任务状态</Text>
            {tasksLoading ? (
              <div style={{ textAlign: 'center', padding: 40 }}>
                <Spin />
              </div>
            ) : tasks.length > 0 ? (
              <div style={{ marginTop: 16 }}>
                <Space direction="vertical" style={{ width: '100%' }}>
                  <div>
                    <Tag color="processing" icon={<ThunderboltOutlined />}>
                      运行中
                    </Tag>
                    <Text strong style={{ marginLeft: 8 }}>
                      {tasks[0].task_name}
                    </Text>
                  </div>
                  <Text type="secondary">
                    地区: {tasks[0].region} | 进度: {tasks[0].progress}
                  </Text>
                  {tasks[0].current_dataset && (
                    <Text type="secondary">
                      数据集: {tasks[0].current_dataset}
                    </Text>
                  )}
                  {tasks[0].current_step && (
                    <Tag color="blue">{tasks[0].current_step}</Tag>
                  )}
                </Space>
              </div>
            ) : (
              <div style={{ textAlign: 'center', padding: 40 }}>
                <Text type="secondary">暂无活跃任务</Text>
              </div>
            )}
          </Card>
        </Col>

        {/* System Health Card */}
        <Col xs={24} sm={24} lg={8}>
          <Card className="glass-card">
            <Text type="secondary">系统健康状态</Text>
            <div style={{ marginTop: 16 }}>
              <Space direction="vertical" style={{ width: '100%' }}>
                <div style={{ display: 'flex', justifyContent: 'space-between' }}>
                  <Text>BRAIN 平台连接</Text>
                  <Tag color="success" icon={<CheckCircleOutlined />}>已连接</Tag>
                </div>
                <div style={{ display: 'flex', justifyContent: 'space-between' }}>
                  <Text>LLM 服务</Text>
                  <Tag color="success" icon={<CheckCircleOutlined />}>在线</Tag>
                </div>
                <div style={{ display: 'flex', justifyContent: 'space-between' }}>
                  <Text>数据库</Text>
                  <Tag color="success" icon={<CheckCircleOutlined />}>已连接</Tag>
                </div>
              </Space>
            </div>
          </Card>
        </Col>
      </Row>

      {/* Second Row: KPI Cards */}
      <Row gutter={[16, 16]} style={{ marginTop: 16 }}>
        <Col xs={12} sm={6}>
          <Card className="glass-card">
            <Statistic
              title="今日模拟次数"
              value={metrics.today_simulations}
              prefix={<ClockCircleOutlined />}
              valueStyle={{ color: '#00d4ff' }}
            />
          </Card>
        </Col>
        <Col xs={12} sm={6}>
          <Card className="glass-card">
            <Statistic
              title="成功率"
              value={metrics.today_success_rate * 100}
              precision={1}
              suffix="%"
              valueStyle={{ color: '#00ff88' }}
            />
          </Card>
        </Col>
        <Col xs={12} sm={6}>
          <Card className="glass-card">
            <Statistic
              title="平均夏普比率"
              value={metrics.today_avg_sharpe}
              precision={2}
              prefix={<LineChartOutlined />}
              valueStyle={{ color: '#ffb700' }}
            />
          </Card>
        </Col>
        <Col xs={12} sm={6}>
          <Card className="glass-card">
            <Statistic
              title="本周 Alpha 总数"
              value={metrics.week_total_alphas}
              prefix={<CheckCircleOutlined />}
              valueStyle={{ color: '#9c88ff' }}
            />
          </Card>
        </Col>
      </Row>

      {/* Third Row: Live Feed + PnL Chart */}
      <Row gutter={[16, 16]} style={{ marginTop: 16 }}>
        {/* Live Activity Feed */}
        <Col xs={24} lg={12}>
          <Card 
            className="glass-card" 
            title="实时活动动态"
            style={{ height: 400 }}
          >
            <div style={{ height: 320, overflow: 'auto' }}>
              <List
                size="small"
                dataSource={liveFeed.length > 0 ? liveFeed : [
                  { message: '⏳ 等待活动...', timestamp: new Date().toISOString() }
                ]}
                renderItem={(item) => (
                  <List.Item className="feed-item" style={{ 
                    padding: '8px 0',
                    borderBottom: '1px solid rgba(255,255,255,0.05)',
                  }}>
                    <Space>
                      <Text type="secondary" style={{ fontSize: 12 }}>
                        {item.timestamp ? new Date(item.timestamp).toLocaleTimeString() : '--:--:--'}
                      </Text>
                      <Text>{item.message}</Text>
                    </Space>
                  </List.Item>
                )}
              />
            </div>
          </Card>
        </Col>

        {/* PnL Chart */}
        <Col xs={24} lg={12}>
          <Card 
            className="glass-card" 
            title="Top 10 Alpha 累计收益"
            style={{ height: 400 }}
          >
            <ResponsiveContainer width="100%" height={320}>
              <LineChart data={mockPnLData}>
                <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.1)" />
                <XAxis dataKey="date" stroke="rgba(255,255,255,0.5)" />
                <YAxis stroke="rgba(255,255,255,0.5)" unit="%" />
                <Tooltip 
                  contentStyle={{ 
                    background: '#131a2b', 
                    border: '1px solid rgba(255,255,255,0.1)',
                    borderRadius: 8,
                  }}
                />
                <Line 
                  type="monotone" 
                  dataKey="returns" 
                  stroke="#00ff88" 
                  strokeWidth={2}
                  dot={false}
                />
              </LineChart>
            </ResponsiveContainer>
          </Card>
        </Col>
      </Row>
    </div>
  )
}
