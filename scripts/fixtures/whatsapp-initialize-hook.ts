/** Observe SDK injection while preserving the compiled host's full initialize call. */
export function observeWhatsAppInitialize(host: any, onInjected: () => void) {
  const initialize = host.initialize.bind(host);
  host.initialize = async (...args: any[]) => {
    const client = args[0];
    const inject = client.inject.bind(client);
    client.inject = async () => { await inject(); onInjected(); };
    return initialize(...args);
  };
}
