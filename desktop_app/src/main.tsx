import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import { Toaster } from 'sonner'
import './styles/globals.css'
import App from './App'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <BrowserRouter>
      <App />
      <Toaster
        theme="dark"
        position="bottom-right"
        duration={2000}
        toastOptions={{
          style: {
            background: 'oklch(0.16 0.018 286 / 0.85)',
            backdropFilter: 'blur(20px)',
            border: '1px solid oklch(1 0 0 / 0.10)',
            color: 'white',
          },
        }}
      />
    </BrowserRouter>
  </StrictMode>,
)
