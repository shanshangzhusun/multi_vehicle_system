from scripts.apply_deployment_config import deployment_vehicle_ports, theater_for_port, vehicle_advertise_host_for_port


def test_vehicle_host_mapping_uses_local_vehicle_host() -> None:
    deployment = {
        "vehicles": {
            "host_mappings": [
                {"start": 8414, "end": 8477, "host": "192.168.2.8"},
                {"start": 8519, "end": 8582, "host": "192.168.2.8"},
            ]
        }
    }

    assert vehicle_advertise_host_for_port(deployment, 8414, "192.168.2.8") == "192.168.2.8"
    assert vehicle_advertise_host_for_port(deployment, 8477, "192.168.2.8") == "192.168.2.8"
    assert vehicle_advertise_host_for_port(deployment, 8519, "192.168.2.8") == "192.168.2.8"
    assert vehicle_advertise_host_for_port(deployment, 8582, "192.168.2.8") == "192.168.2.8"
    assert vehicle_advertise_host_for_port(deployment, 9000, "192.168.2.8") == "192.168.2.8"


def test_theater_vehicle_port_ranges_support_non_contiguous_groups() -> None:
    deployment = {
        "theaters": [
            {"id": "26D-A", "vehicle_port_ranges": [{"start": 8414, "end": 8449}]},
            {"id": "26D-B", "vehicle_port_ranges": [{"start": 8450, "end": 8477}, {"start": 8605, "end": 8612}]},
            {"id": "27-B", "vehicle_port_ranges": ["8555-8582", "8613-8617"]},
        ]
    }

    ports = deployment_vehicle_ports(deployment)

    assert len(ports) == 105
    assert 8485 not in ports
    assert 8605 in ports
    assert theater_for_port(deployment["theaters"], 8611)["id"] == "26D-B"
    assert theater_for_port(deployment["theaters"], 8617)["id"] == "27-B"
