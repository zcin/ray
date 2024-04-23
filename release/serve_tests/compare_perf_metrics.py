import click
import json
from typing import Dict


def load_metrics(file_path: str) -> Dict[str, float]:
    with open(file_path) as f:
        report = json.load(f)
        if "perf_metrics" in report:
            # Input file is the TEST_OUTPUT_JSON (e.g. /tmp/release_test_out.json)
            # that is written to directly from running `python workloads/abc.py`
            results = report
        elif "results" in report:
            # Input file is either downloaded from the buildkite job artifacts
            # or from file uploaded to AWS
            results = report["results"]
        else:
            raise RuntimeError(f"Invalid results file {file_path}")

        return {
            perf_metric["perf_metric_name"]: perf_metric["perf_metric_value"]
            for perf_metric in results["perf_metrics"]
        }


@click.command()
@click.argument("results_file1")
@click.argument("results_file2")
def main(results_file1: str, results_file2: str):
    metrics1 = load_metrics(results_file1)
    metrics2 = load_metrics(results_file2)

    print("|metric                        |results 1      |results 2      |%change   |")
    print("|------------------------------|---------------|---------------|----------|")
    for key in metrics1:
        if key in metrics2:
            change = metrics2[key] / metrics1[key] - 1
            percent_change = str(round(100 * change, 2))

            metric1 = str(round(metrics1[key], 2))
            metric2 = str(round(metrics2[key], 2))

            print(f"|{key.ljust(30)}", end="")
            print(f"|{metric1.ljust(15)}", end="")
            print(f"|{metric2.ljust(15)}", end="")
            print(f"|{percent_change.ljust(10)}|")


if __name__ == "__main__":
    main()
