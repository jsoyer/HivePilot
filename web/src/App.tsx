import { Pollen } from '@/components/Pollen'
import { TokenGate } from '@/components/TokenGate'

function App() {
  return (
    <TokenGate>
      <Pollen />
    </TokenGate>
  )
}

export default App
