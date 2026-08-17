@echo on
set RUN_PARAM=%1

@REM @REM python combine.py --LFC ".\%RUN_PARAM%\outLFC.txt" --HFC ".\%RUN_PARAM%\outHFC.txt" --out ".\%RUN_PARAM%\\"
python moo.py -i ".\manual_cropped_soundsInregistrari vitei 3 august\\%RUN_PARAM%\HFC\\" -o ".\manual_cropped_sounds\8_01_date\%RUN_PARAM%\output.csv" -a calf -c HFC --isolate --retries 20
python moo.py -i ".\manual_cropped_sounds\8_01_date\%RUN_PARAM%\LFC\\" -o ".\manual_cropped_sounds\8_01_date\%RUN_PARAM%\output.csv" -a calf -c LFC --isolate --retries 20 --append
@REM @REM python compare.py -orig ".\%RUN_PARAM%\%RUN_PARAM%.xlsx" -new ".\%RUN_PARAM%\output.csv"
