import { ConfigProvider } from 'antd'
import Layout from './components/Layout'

function App() {
  return (
    <ConfigProvider
      theme={{
        token: {
          colorPrimary: '#1677ff',
          borderRadius: 6,
        },
      }}
    >
      <Layout />
    </ConfigProvider>
  )
}

export default App
