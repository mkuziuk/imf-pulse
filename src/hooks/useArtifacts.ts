import { useEffect, useMemo, useState } from "react";
import { loadArtifactManifests, type ArtifactLoadResult } from "../lib/artifacts";

interface ArtifactState extends ArtifactLoadResult {
  loading: boolean;
}

export function useArtifacts(urls: string[]): ArtifactState {
  const key = useMemo(() => [...new Set(urls)].sort().join("\n"), [urls]);
  const [state, setState] = useState<ArtifactState>({
    artifacts: [],
    issues: [],
    loading: true
  });

  useEffect(() => {
    let active = true;
    const manifestUrls = key ? key.split("\n") : [];
    if (manifestUrls.length === 0) {
      setState({ artifacts: [], issues: [], loading: false });
      return () => {
        active = false;
      };
    }
    setState((current) => ({ ...current, loading: true }));
    void loadArtifactManifests(manifestUrls).then((result) => {
      if (active) setState({ ...result, loading: false });
    });
    return () => {
      active = false;
    };
  }, [key]);

  return state;
}
