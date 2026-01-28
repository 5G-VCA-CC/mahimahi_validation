sudo -E bash -lc '
set -e

# kill anything holding old netns open
ip netns pids ns_s 2>/dev/null | xargs -r kill -9 || true
ip netns pids ns_r 2>/dev/null | xargs -r kill -9 || true

# delete namespaces (removes their interfaces too)
ip netns del ns_s 2>/dev/null || true
ip netns del ns_r 2>/dev/null || true

# remove stale netns files if they linger
rm -f /var/run/netns/ns_s /var/run/netns/ns_r 2>/dev/null || true

# remove any leftover veth in root namespace
ip link del veth-s 2>/dev/null || true
ip link del veth-r 2>/dev/null || true

# show what's left
echo "---- netns ----"
ip netns list || true
echo "---- links (veth) ----"
ip -o link show | grep -E "veth-s|veth-r" || true
'
