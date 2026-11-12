import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import '../index.css'
import App from './App.tsx'
import { applyUiScale, readUiScale } from './scale'

// 先套用界面缩放再渲染，免得先闪一下小字再变大
applyUiScale(readUiScale())

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
