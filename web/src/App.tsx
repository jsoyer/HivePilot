import { Mirador } from '@/components/Mirador'
import { TokenGate } from '@/components/TokenGate'

function App() {
  return (
    <TokenGate>
      <Mirador />
    </TokenGate>
  )
}

export default App
