import { useEffect, useState } from 'react'
import Particles, { initParticlesEngine } from '@tsparticles/react'
import { loadConfettiPreset } from '@tsparticles/preset-confetti'

/**
 * Fires confetti once on mount. Cleans up automatically.
 * Used to celebrate STRONG-tier role discovery.
 */
export function Confetti({ trigger }: { trigger: boolean }) {
  const [ready, setReady] = useState(false)

  useEffect(() => {
    initParticlesEngine(async (engine) => {
      await loadConfettiPreset(engine)
    }).then(() => setReady(true))
  }, [])

  if (!ready || !trigger) return null

  return (
    <Particles
      id="confetti"
      options={{
        preset: 'confetti',
        fullScreen: { enable: true, zIndex: 9999 },
        emitters: {
          life: { count: 1, duration: 0.4 },
          rate: { delay: 0.05, quantity: 100 },
          position: { x: 50, y: 30 },
        },
        particles: {
          color: {
            value: ['#5e8aff', '#9a6dff', '#5af2c0', '#ffd96a'],
          },
        },
      }}
    />
  )
}
