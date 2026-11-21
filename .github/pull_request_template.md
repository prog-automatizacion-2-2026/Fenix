## Qué hace este PR

<!-- Rol, módulo tocado y resumen breve de los cambios -->

## Cómo probarlo

<!-- Comando(s) para reproducir y resultado esperado; IDs de la matriz (F01–F16) que cubre -->

## Checklist

- [ ] Solo toqué archivos de mi módulo (o avisé si no)
- [ ] Las firmas y docstrings (Args, Returns, Raises) coinciden con la plantilla
- [ ] Sin `except: pass`, sin `BaseException`, sin `return/break/continue` en `finally`
- [ ] Operaciones de red con timeout acotado
- [ ] Sin secretos, tokens, firmas completas ni URLs privadas en código o logs
- [ ] `python -m pytest -m base -q` sigue pasando
- [ ] Probado contra LocalStack
