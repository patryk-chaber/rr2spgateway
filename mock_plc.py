#!/usr/bin/env python3
"""
Simple mock SLMP server for testing.
Supports multiple ports, each with single connection limit.
All ports share the same memory.
"""

import socket
import threading
import time
from struct import pack, unpack

class MockPLC:
    """Mock PLC that responds to SLMP requests."""
    
    def __init__(self, ports=None, gain=1.0, time_constant=10.0):
        """
        Initialize mock PLC.

        Args:
            ports:         List of ports to listen on (each allows 1 connection)
            gain:          Static gain K of the simulated first-order system
            time_constant: Time constant T [s] of the simulated first-order system
        """
        self.ports = list(ports) if ports is not None else [30000]
        self.running = False
        self.server_sockets = {}
        self.active_connections = {}  # port -> client_socket

        # Shared memory across all ports (register -> value)
        self.memory = {
            **{f'D{i}':  0 for i in range(100, 201)},
            **{f'SD{i}': 0 for i in range(510, 601)},
        }

        # Lock for memory access (thread-safe)
        self.memory_lock = threading.Lock()

        # Single-inertia simulation parameters (input: D114, output: D100)
        self.gain = gain
        self.time_constant = time_constant
        self._sim_state = 0.0   # continuous internal state (float)
    
    def set_register(self, register, value):
        """Set a register value (thread-safe)."""
        with self.memory_lock:
            self.memory[register] = value
            print(f"  Set {register} = {value}")
    
    def get_register(self, register):
        """Get a register value (thread-safe)."""
        with self.memory_lock:
            return self.memory.get(register, 0)
    
    def start(self):
        """Start the mock PLC server on all ports."""
        self.running = True
        self.start_time = time.monotonic()

        print("Mock PLC starting...")
        print(f"Ports: {self.ports} (1 connection each)")
        print(f"Shared memory: {self.memory}\n")

        threading.Thread(target=self._run_clock, daemon=True).start()
        threading.Thread(target=self._run_simulation, daemon=True).start()

        # Start a server thread for each port
        for port in self.ports:
            thread = threading.Thread(
                target=self._run_server,
                args=(port,),
                daemon=True
            )
            thread.start()
    
    def _run_clock(self):
        """Update SD518 with elapsed seconds since start."""
        while self.running:
            elapsed = int(time.monotonic() - self.start_time)
            self.set_register('SD518', elapsed)
            time.sleep(1)

    def _run_simulation(self, dt=0.1):
        """Simulate a first-order (single inertia) system at dt-second intervals.

        Input  register: D114
        Output register: D100
        Update rule: y[k+1] = y[k] + (dt/T) * (K*u[k] - y[k])
        """
        while self.running:
            u = self.get_register('D114')
            self._sim_state += (dt / self.time_constant) * (self.gain * u - self._sim_state)
            self.set_register('D100', int(self._sim_state))
            time.sleep(dt)

    def _run_server(self, port):
        """Run server for a specific port."""
        try:
            # Create server socket
            server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server_socket.bind(('127.0.0.1', port))
            server_socket.listen(1)  # Accept only 1 connection
            
            self.server_sockets[port] = server_socket
            print(f"[Port {port}] Listening...")
            
            while self.running:
                try:
                    server_socket.settimeout(1.0)
                    
                    # Check if port already has active connection
                    if port in self.active_connections:
                        continue
                    
                    # Accept new connection
                    client_socket, addr = server_socket.accept()
                    print(f"[Port {port}] Connection from {addr}")
                    
                    # Store active connection
                    self.active_connections[port] = client_socket
                    
                    # Handle this client
                    self._handle_client(port, client_socket)
                    
                except socket.timeout:
                    continue
                except Exception as e:
                    if self.running:
                        print(f"[Port {port}] Accept error: {e}")
                        
        except Exception as e:
            print(f"[Port {port}] Server error: {e}")
        finally:
            if port in self.server_sockets:
                self.server_sockets[port].close()
    
    def _handle_client(self, port, client_socket):
        """Handle SLMP requests from a client."""
        try:
            while self.running:
                # Receive SLMP request
                request = client_socket.recv(1024)
                if not request:
                    break
                
                print(f"[Port {port}] Received {len(request)} bytes")
                
                # Parse and respond
                response = self._process_request(request)
                if response:
                    client_socket.send(response)
                    print(f"[Port {port}] Sent {len(response)} bytes")
                
        except ConnectionResetError:
            print(f"[Port {port}] Connection reset by client")
        except Exception as e:
            print(f"[Port {port}] Client error: {e}")
        finally:
            client_socket.close()
            # Remove from active connections
            if port in self.active_connections:
                del self.active_connections[port]
            print(f"[Port {port}] Connection closed - port available")
    
    def _process_request(self, request):
        """Process SLMP request and generate response."""
        try:
            if len(request) < 23:
                return None
            
            # Extract key fields
            command = unpack('<H', request[11:13])[0]
            device_code = unpack('<H', request[19:21])[0]
            register_no = unpack('<I', request[15:19])[0]
            num_points = unpack('<H', request[21:23])[0]
            
            if command == 0x0401:
                return self._build_read_response(request, device_code, register_no, num_points)
            elif command == 0x1401:
                return self._build_write_response(request, device_code, register_no, num_points)
            else:
                return None
                
        except Exception as e:
            print(f"  Parse error: {e}")
            return None
    
    def _build_read_response(self, request, device_code, register_no, num_points):
        """Build SLMP read response."""
        # Copy request header (first 7 bytes)
        response_header = request[0:7]
        
        # Calculate data length
        data_length = 2 + num_points * 2
        data_length_bytes = pack('<H', data_length)
        end_code_bytes = pack('<H', 0x0000)  # Success
        
        # Map device code to register prefix
        device_map = {
            0x00A8: 'D',    # D registers
            0x00A9: 'SD',   # SD registers
        }
        device_prefix = device_map.get(device_code, 'D')
        
        # Read consecutive registers
        data_bytes = b''
        for i in range(num_points):
            register_name = f"{device_prefix}{register_no + i}"
            value = self.get_register(register_name)
            data_bytes += pack('<H', value)
        
        return response_header + data_length_bytes + end_code_bytes + data_bytes
    
    def _build_write_response(self, request, device_code, register_no, num_points):
        """Build SLMP write response and update memory."""
        device_map = {
            0x00A8: 'D',
            0x00A9: 'SD',
        }
        device_prefix = device_map.get(device_code, 'D')

        for i in range(num_points):
            value, = unpack('<H', request[23 + i*2 : 25 + i*2])
            register_name = f"{device_prefix}{register_no + i}"
            self.set_register(register_name, value)

        response_header = request[0:7]
        data_length_bytes = pack('<H', 2)
        end_code_bytes = pack('<H', 0x0000)
        return response_header + data_length_bytes + end_code_bytes

    def stop(self):
        """Stop the mock PLC server."""
        print("\nStopping mock PLC...")
        self.running = False
        
        # Close all active connections
        for port, client_socket in list(self.active_connections.items()):
            try:
                client_socket.close()
            except OSError:
                pass
        
        # Close all server sockets
        for port, server_socket in self.server_sockets.items():
            try:
                server_socket.close()
            except OSError:
                pass
        
        print("Mock PLC stopped.")


if __name__ == '__main__':
    # Example: Run on 6 ports, each allows 1 connection
    ports=[30000, 30001, 30002, 30003, 30004, 30005];
    mock = MockPLC(ports)
    
    # Set some test values (shared across all ports)
    mock.set_register('D100', 1234)
    mock.set_register('D101', 5678)
    mock.set_register('SD519', 42)
    
    mock.start()
    
    print(f"\nMock PLC is running on {len(ports)} ports.")
    print("Each port allows 1 connection at a time.")
    print("All ports share the same memory.\n")
    print(f"Ports: {ports}")
    print("Press Ctrl+C to stop.\n")
    
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        mock.stop()