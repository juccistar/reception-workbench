import { windowsJobProgram } from '../../../../scripts/windows-job.mjs';

/** The shared Windows job primitive owns descendants before their code runs. */
export const installerJobProgram = windowsJobProgram.replaceAll('ReceptionProcessJob', 'ReceptionInstallerJob');
