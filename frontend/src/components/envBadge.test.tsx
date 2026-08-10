/** DEV badge (#568): rozsvítí se jen ve vývojovém prostředí. */
import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { EnvBadge } from './Sidebar'

describe('EnvBadge', () => {
  it('v dev prostředí ukáže DEV', () => {
    render(<EnvBadge env="dev" />)
    expect(screen.getByText('DEV')).toBeTruthy()
  })

  it('v produkci (prázdný env) nevykreslí nic', () => {
    const { container } = render(<EnvBadge env="" />)
    expect(container.childElementCount).toBe(0)
  })

  it('neznámá hodnota se chová jako produkce', () => {
    const { container } = render(<EnvBadge env="staging" />)
    expect(container.childElementCount).toBe(0)
  })
})
