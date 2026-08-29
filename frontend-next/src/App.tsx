import { Navigate, Route, Routes } from "react-router-dom";
import { GlobalHeader } from "./components/GlobalHeader";
import { Home } from "./routes/Home";
import { MockRoom } from "./routes/MockRoom";
import { ProblemList } from "./routes/ProblemList";
import { ProblemWorkspace } from "./routes/ProblemWorkspace";
import { ProgressBoard } from "./routes/ProgressBoard";

export default function App() {
  return (
    <div style={{ minHeight: "100vh", display: "flex", flexDirection: "column" }}>
      <GlobalHeader />
      <div style={{ flex: 1, minHeight: 0 }}>
        <Routes>
          <Route path="/" element={<Home />} />
          <Route path="/practice/:track" element={<ProblemList />} />
          <Route path="/practice/:track/:problemId" element={<ProblemWorkspace />} />
          <Route path="/mock" element={<MockRoom />} />
          <Route path="/progress" element={<ProgressBoard />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </div>
    </div>
  );
}
