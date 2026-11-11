!macro customInstall
  CreateDirectory "$INSTDIR\resources"
  FileOpen $0 "$INSTDIR\resources\reception-installed.json.tmp" w
  IfErrors marker_failed
  FileWrite $0 '{"format":1,"app_id":"local.inquiry.reception.workbench","version":"${VERSION}"}'
  FileClose $0
  System::Call 'kernel32::MoveFileExW(w "$INSTDIR\resources\reception-installed.json.tmp", w "$INSTDIR\resources\reception-installed.json", i 9) i .r0'
  IntCmp $0 0 marker_failed marker_done marker_done
  marker_failed:
    Delete "$INSTDIR\resources\reception-installed.json.tmp"
    Abort "Cannot write installation marker."
  marker_done:
!macroend
