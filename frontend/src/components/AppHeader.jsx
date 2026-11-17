import { Layout, Space, Badge, Avatar, Dropdown } from 'antd'
import { BellOutlined, UserOutlined, QuestionCircleOutlined } from '@ant-design/icons'

const { Header } = Layout

export default function AppHeader() {
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
        
        <Badge count={3} size="small">
          <BellOutlined style={{ fontSize: 18, color: 'rgba(255, 255, 255, 0.65)' }} />
        </Badge>
        
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
