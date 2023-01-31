"""The following imports are necessary for updating the registry"""
try:
    import frontier_exploration.frontier_detection
    import frontier_exploration.measurements
    import frontier_exploration.policy
    import frontier_exploration.base_explorer
    import frontier_exploration.objnav_explorer
    import frontier_exploration.trainer
except ModuleNotFoundError as e:
    # If the error was due to the habitat package not being installed, then pass, but
    # print a warning. Do not pass if it was due to another package being missing.
    if e.name != "habitat":
        raise e
    else:
        print(
            "Warning: habitat package not installed. Cannot register habitat_baselines "
            "components."
        )

