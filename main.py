from regression_impact.setup_flow import run_setup

def main() -> int:
  try:
    context = run_setup()
    return 0
  except KeyboardInterrupt:
    print("\n\nSetup cancelled.")
    return 130

if __name__ == "__main__":
  raise SystemExit(main())